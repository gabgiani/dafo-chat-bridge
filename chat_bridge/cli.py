from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from . import __version__, claude, codex, copilot
from .model import limit_turns, render_markdown
from .paths import chat_bridge_home

SOURCES = {'copilot': copilot, 'claude': claude, 'codex': codex}
EXPORT_DIR = chat_bridge_home() / 'exports'


def tool_output_arg(value: str):
    if value == 'none':
        return None
    if value == 'full':
        return -1
    return int(value)


def slug(text: str) -> str:
    return re.sub(r'[^a-zA-Z0-9]+', '-', text).strip('-')[:60] or 'chat'


def in_dir(cwd, folder: str) -> bool:
    if not cwd:
        return False
    cwd, folder = os.path.realpath(cwd), os.path.realpath(folder)
    if cwd == folder or cwd.startswith(folder + os.sep):
        return True
    # project moved: the recorded directory no longer exists but has the same folder name
    return not os.path.exists(cwd) and os.path.basename(cwd) == os.path.basename(folder)


def cmd_list(args):
    rows = []
    for name in ([args.source] if args.source else SOURCES):
        rows += SOURCES[name].list_sessions()
    if args.cwd:
        rows = [r for r in rows if in_dir(r['cwd'], args.cwd)]
    if args.search:
        needle = args.search.lower()
        rows = [r for r in rows if needle in (r['title'] or '').lower() or needle in (r['cwd'] or '').lower()]
    rows.sort(key=lambda r: r['updated'] or 0, reverse=True)
    if args.json:
        print(json.dumps(rows[:args.limit], ensure_ascii=False))
        return
    for r in rows[:args.limit]:
        when = datetime.fromtimestamp(r['updated'] / 1000).strftime('%Y-%m-%d %H:%M')
        print(f"{r['source']:<8} {r['id']:<36} {when} {r['size'] / 1e6:7.1f}MB  {r['title'][:70]}  [{r['cwd'] or '-'}]")


def load(args):
    if args.source == 'copilot':
        conv = copilot.read(args.id, args.skip_participant)
    else:
        conv = SOURCES[args.source].read(args.id)
    return limit_turns(conv, args.last)


def cmd_show(args):
    sys.stdout.write(render_markdown(load(args), args.tool_output))


def cmd_active_copilot(args):
    meta = copilot.active(Path(args.ws_dir), args.participant, args.since)
    print(json.dumps(meta, ensure_ascii=False))
    if meta is None:
        sys.exit(2)


def cmd_export(args):
    conv = load(args)
    if not conv.turns:
        raise SystemExit('La conversación no tiene turnos para migrar.')
    title = args.title or f'{conv.title} (desde {conv.source})'
    cwd = args.cwd or conv.cwd or os.getcwd()
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    result = {'target': args.to, 'title': title, 'cwd': cwd, 'turns': len(conv.turns), 'id': None, 'path': None}
    if args.to == 'md':
        out = Path(args.out) if args.out else EXPORT_DIR / f'{slug(title)}.md'
        out.write_text(render_markdown(conv, args.tool_output), encoding='utf-8')
        result['path'] = str(out)
        message = f'Markdown: {out}'
    elif args.to == 'copilot':
        out = Path(args.out) if args.out else EXPORT_DIR / f'{slug(title)}.copilot.json'
        out.write_text(json.dumps(copilot.to_import_json(conv, args.tool_output), ensure_ascii=False, indent=1), encoding='utf-8')
        result['path'] = str(out)
        message = f'Archivo de importación: {out}\nEn VS Code: Command Palette > "Chat: Import Chat..." y selecciona ese archivo.'
    elif args.to == 'claude':
        result['id'] = claude.write(conv, cwd, args.tool_output, title)
        message = (f"Sesión Claude Code creada: {result['id']}  (cwd {cwd})\n"
                   f"Continuar: cd \"{cwd}\" && claude --resume {result['id']}   (o desde el historial de la extensión)")
    else:
        result['id'] = codex.write(conv, cwd, args.tool_output, title)
        message = (f"Hilo Codex creado: {result['id']}  (cwd {cwd})\n"
                   f"Continuar: codex resume {result['id']}   (o desde el historial de la extensión/app)")
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(message)
        print(f'{len(conv.turns)} turnos migrados.')


def main(argv=None):
    parser = argparse.ArgumentParser(prog='chat-bridge', description='DAFO Chat Bridge: lista y migra chats entre GitHub Copilot (VS Code), Claude Code y Codex.')
    parser.add_argument('--version', action='version', version=f'DAFO Chat Bridge {__version__}')
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('list', help='lista sesiones')
    p.add_argument('--source', choices=SOURCES)
    p.add_argument('--cwd', help='solo sesiones de este directorio (incluye subcarpetas y proyectos movidos con el mismo nombre)')
    p.add_argument('--search', help='filtra por texto en título o directorio')
    p.add_argument('--limit', type=int, default=40)
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_list)

    p = sub.add_parser('active-copilot', help='sesión de Copilot activa en un workspace (uso interno de la extensión)')
    p.add_argument('--ws-dir', required=True, help='carpeta workspaceStorage/<hash>')
    p.add_argument('--participant', required=True, help='id del participante cuyo request cierra la sesión')
    p.add_argument('--since', type=int, required=True, help='epoch ms mínimo del último request')
    p.set_defaults(func=cmd_active_copilot)

    for name, func, help_text in (('show', cmd_show, 'muestra una sesión en Markdown'),
                                  ('export', cmd_export, 'migra una sesión a otro agente o a Markdown')):
        p = sub.add_parser(name, help=help_text)
        p.add_argument('source', choices=SOURCES)
        p.add_argument('id', help='id de sesión (o prefijo único); en copilot también la ruta del archivo')
        p.add_argument('--skip-participant', help='copilot: omite los requests enviados a este participante')
        p.add_argument('--tool-output', type=tool_output_arg, default=500, metavar='N|full|none',
                       help='caracteres de salida por herramienta (default 500; full = completo; none = sin herramientas; 0 = solo comandos)')
        p.add_argument('--last', type=int, help='solo los últimos N turnos')
        p.set_defaults(func=func)
        if name == 'export':
            p.add_argument('--to', required=True, choices=('copilot', 'claude', 'codex', 'md'))
            p.add_argument('--cwd', help='directorio de trabajo destino (default: el de la sesión origen)')
            p.add_argument('--title', help='título de la sesión destino')
            p.add_argument('--out', help='archivo de salida (md / copilot)')
            p.add_argument('--json', action='store_true', help='resultado en JSON')

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == '__main__':
    main()

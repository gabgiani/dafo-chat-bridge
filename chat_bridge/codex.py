from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .model import Conversation, ToolCall, Turn, iso_to_ms, ms_to_iso, now_ms, render_assistant, uuid7
from .paths import codex_home, find_binary

HOME = codex_home()
SESSION_DIRS = (HOME / 'sessions', HOME / 'archived_sessions')
IDE_REQUEST_MARKER = '## My request for Codex:'
INJECTED_TAG = re.compile(r'^<([a-z_]+)>.*</\1>$', re.S)
INJECTED_PREFIXES = ('<environment_context>', '# AGENTS.md instructions', '<user_instructions>',
                     '<permissions instructions>', '<turn_aborted>', '<subagent_notification>', '<skill>')


def binary() -> Optional[str]:
    return find_binary('codex', 'CHAT_BRIDGE_CODEX_BIN', 'openai.chatgpt-*/bin/*')


def _codex(*args: str) -> subprocess.CompletedProcess:
    exe = binary()
    if not exe:
        raise SystemExit('codex: no se encontró el binario de Codex (instala Codex o define CHAT_BRIDGE_CODEX_BIN)')
    return subprocess.run([exe, *args], capture_output=True, text=True)


def _state_db() -> Optional[Path]:
    dbs = sorted(HOME.glob('state_*.sqlite'), key=lambda p: int(p.stem.split('_')[1]))
    return dbs[-1] if dbs else None


def _threads() -> Dict[str, dict]:
    db = _state_db()
    if not db:
        return {}
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    try:
        return {r['id']: dict(r) for r in con.execute('select id, rollout_path, cwd, title, name, updated_at_ms from threads')}
    finally:
        con.close()


def _rollouts() -> Dict[str, Path]:
    out = {}
    for root in SESSION_DIRS:
        for f in root.rglob('rollout-*.jsonl'):
            out[f.stem[-36:]] = f
    return out


def list_sessions() -> List[dict]:
    threads = _threads()
    out = []
    for sid, f in _rollouts().items():
        if f.stat().st_size == 0:
            continue
        t = threads.get(sid)
        if t:
            title, cwd, updated = t['name'] or t['title'], t['cwd'], t['updated_at_ms']
        else:
            with f.open('rb') as fh:
                meta = json.loads(fh.readline()).get('payload') or {}
            title, cwd, updated = '(sin índice)', meta.get('cwd'), None
        out.append({'source': 'codex', 'id': sid, 'title': (title or '(sin título)').split('\n')[0][:80], 'cwd': cwd,
                    'updated': updated or int(f.stat().st_mtime * 1000), 'size': f.stat().st_size, 'path': str(f)})
    return out


def find(session_id: str) -> dict:
    matches = [s for s in list_sessions() if s['id'].startswith(session_id)]
    if len(matches) != 1:
        raise SystemExit(f'codex: {len(matches)} sesiones coinciden con "{session_id}"')
    return matches[0]


def _joined(content, kinds) -> str:
    if isinstance(content, str):
        return content
    return '\n'.join(c.get('text', '') for c in content or [] if isinstance(c, dict) and c.get('type') in kinds)


def _user_text(payload: dict) -> Optional[str]:
    text = _joined(payload.get('content'), ('input_text',)).strip()
    if IDE_REQUEST_MARKER in text:
        return text.split(IDE_REQUEST_MARKER, 1)[1].strip()
    if not text or text.startswith(INJECTED_PREFIXES) or INJECTED_TAG.match(text):
        return None
    return text


def _call_detail(payload: dict) -> str:
    kind = payload.get('type')
    if kind == 'function_call':
        try:
            args = json.loads(payload.get('arguments') or '{}')
        except json.JSONDecodeError:
            return payload.get('arguments') or ''
        cmd = args.get('cmd') or args.get('command')
        if isinstance(cmd, list):
            cmd = ' '.join(cmd)
        return cmd or json.dumps(args, ensure_ascii=False)
    if kind == 'custom_tool_call':
        return payload.get('input') or ''
    if kind == 'local_shell_call':
        return ' '.join((payload.get('action') or {}).get('command') or [])
    if kind == 'web_search_call':
        return (payload.get('action') or {}).get('query') or ''
    return ''


def _output_text(output) -> str:
    if isinstance(output, dict):
        output = output.get('content', output.get('output', ''))
    return _joined(output, ('input_text', 'output_text', 'text'))


def read(session_id: str) -> Conversation:
    meta = find(session_id)
    turns: List[Turn] = []
    calls: Dict[str, ToolCall] = {}
    created = None
    with open(meta['path'], 'rb') as fh:
        for line in fh:
            entry = json.loads(line)
            if entry.get('type') != 'response_item':
                if entry.get('type') == 'session_meta':
                    created = iso_to_ms(entry['payload']['timestamp'])
                continue
            p = entry['payload']
            ts = iso_to_ms(entry['timestamp']) if entry.get('timestamp') else None
            kind = p.get('type')
            if kind == 'message' and p.get('role') == 'user':
                text = _user_text(p)
                if text:
                    turns.append(Turn(user=text, timestamp=ts))
                continue
            if kind in ('function_call_output', 'custom_tool_call_output'):
                if p.get('call_id') in calls:
                    calls[p['call_id']].output = _output_text(p.get('output'))
                continue
            if kind == 'message' and p.get('role') == 'assistant':
                part = _joined(p.get('content'), ('output_text',))
            elif kind in ('function_call', 'custom_tool_call', 'local_shell_call', 'web_search_call'):
                part = ToolCall(p.get('name') or kind, _call_detail(p))
                if p.get('call_id'):
                    calls[p['call_id']] = part
            else:
                continue
            if not turns:
                turns.append(Turn(user='', timestamp=ts))
            turns[-1].parts.append(part)
    return Conversation('codex', meta['id'], meta['title'], meta['cwd'], created, turns)


def write(conv: Conversation, cwd: str, tool_output: Optional[int], title: str) -> str:
    created = now_ms()
    session_id = uuid7(created)
    local = datetime.fromtimestamp(created / 1000)
    folder = HOME / 'sessions' / local.strftime('%Y/%m/%d')
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-{local.strftime('%Y-%m-%dT%H-%M-%S')}-{session_id}.jsonl"
    cli_version = _codex('--version').stdout.split()[-1]
    lines = [{'timestamp': ms_to_iso(created), 'type': 'session_meta',
              'payload': {'id': session_id, 'timestamp': ms_to_iso(created), 'cwd': cwd, 'originator': 'codex_vscode',
                          'cli_version': cli_version, 'source': 'vscode', 'model_provider': 'openai',
                          'base_instructions': None}}]
    for n, turn in enumerate(conv.turns):
        ts = ms_to_iso(created + 2 * n)
        user = turn.user.strip() or '(mensaje sin texto)'
        answer = render_assistant(turn, tool_output) or '(sin respuesta)'
        lines += [
            {'timestamp': ts, 'type': 'response_item',
             'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': user}]}},
            {'timestamp': ts, 'type': 'event_msg', 'payload': {'type': 'user_message', 'message': user, 'images': []}},
            {'timestamp': ts, 'type': 'response_item',
             'payload': {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': answer}]}},
            {'timestamp': ts, 'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': answer}},
        ]
    with path.open('w', encoding='utf-8') as fh:
        for entry in lines:
            fh.write(json.dumps(entry, ensure_ascii=False) + '\n')
    _register_thread(session_id, path, created, cwd, title, cli_version, conv)
    result = _codex('migrate-rollouts', '--thread', session_id, '--apply')
    if result.returncode != 0:
        raise SystemExit(f'codex migrate-rollouts falló para {path}:\n{result.stdout}\n{result.stderr}')
    with (HOME / 'session_index.jsonl').open('a', encoding='utf-8') as fh:
        fh.write(json.dumps({'id': session_id, 'thread_name': title,
                             'updated_at': datetime.utcnow().isoformat() + 'Z'}, ensure_ascii=False) + '\n')
    return session_id


def _register_thread(session_id: str, path: Path, created: int, cwd: str, title: str, cli_version: str, conv: Conversation):
    """migrate-rollouts requires the thread row; settings mirror the user's most recent Codex thread."""
    db = _state_db()
    if not db:
        raise SystemExit('codex: no se encontró state_*.sqlite')
    first_user = next((t.user.strip() for t in conv.turns if t.user.strip()), title)
    con = sqlite3.connect(db, timeout=30)
    try:
        con.row_factory = sqlite3.Row
        latest = con.execute('select sandbox_policy, approval_mode, model, reasoning_effort from threads '
                             'order by updated_at_ms desc limit 1').fetchone()
        if latest is None:
            raise SystemExit('codex: no hay hilos previos de los que tomar sandbox/approval/model')
        con.execute(
            'insert into threads (id, rollout_path, created_at, updated_at, created_at_ms, updated_at_ms, source, '
            'model_provider, cwd, title, sandbox_policy, approval_mode, cli_version, first_user_message, model, '
            'reasoning_effort, thread_source, preview, history_mode, name, originator) '
            "values (?, ?, ?, ?, ?, ?, 'vscode', 'openai', ?, ?, ?, ?, ?, ?, ?, ?, 'user', ?, 'legacy', ?, 'codex_vscode')",
            (session_id, str(path), created // 1000, created // 1000, created, created, cwd, title,
             latest['sandbox_policy'], latest['approval_mode'], cli_version, first_user, latest['model'],
             latest['reasoning_effort'], first_user[:200], title))
        con.commit()
    finally:
        con.close()

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Dict, Iterator, List, Optional
from urllib.parse import urlparse
from urllib.request import url2pathname

from .model import Conversation, ToolCall, Turn, render_assistant
from .paths import vscode_user_dirs

LINK = re.compile(r'\[([^\]]*)\]\((?:file://)?([^)\s]+)\)')


def _uri_path(uri: str) -> str:
    return url2pathname(urlparse(uri).path)


def _workspace_cwd(ws_dir: Path) -> Optional[str]:
    meta = ws_dir / 'workspace.json'
    if not meta.exists():
        return None
    data = json.loads(meta.read_text())
    if 'folder' in data:
        return _uri_path(data['folder'])
    if 'workspace' in data:
        ws_file = Path(_uri_path(data['workspace']))
        if ws_file.exists():
            folders = json.loads(re.sub(r'(?m)^\s*//.*$|,(\s*[}\]])', r'\1', ws_file.read_text())).get('folders') or []
            if folders:
                return str((ws_file.parent / folders[0]['path']).resolve())
        return str(ws_file.parent)
    return None


def _index(ws_dir: Path) -> Dict[str, dict]:
    db = ws_dir / 'state.vscdb'
    if not db.exists():
        return {}
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    try:
        row = con.execute("select value from ItemTable where key='chat.ChatSessionStore.index'").fetchone()
    finally:
        con.close()
    return json.loads(row[0]).get('entries', {}) if row else {}


def _session_dirs() -> Iterator[tuple]:
    for user_dir in vscode_user_dirs():
        ws_root = user_dir / 'workspaceStorage'
        if ws_root.is_dir():
            for ws_dir in ws_root.iterdir():
                if (ws_dir / 'chatSessions').is_dir():
                    yield ws_dir, ws_dir / 'chatSessions'
        empty_window = user_dir / 'globalStorage' / 'emptyWindowChatSessions'
        if empty_window.is_dir():
            yield None, empty_window


def list_sessions() -> List[dict]:
    out = []
    for ws_dir, sessions in _session_dirs():
        index = _index(ws_dir) if ws_dir else {}
        cwd = _workspace_cwd(ws_dir) if ws_dir else None
        for f in sessions.iterdir():
            if f.suffix not in ('.jsonl', '.json'):
                continue
            entry = index.get(f.stem, {})
            if entry.get('isEmpty'):
                continue
            out.append({'source': 'copilot', 'id': f.stem, 'title': entry.get('title') or '(sin título)',
                        'cwd': cwd, 'updated': entry.get('lastMessageDate') or int(f.stat().st_mtime * 1000),
                        'size': f.stat().st_size, 'path': str(f)})
    return out


def _apply(state, entry):
    kind, path = entry['kind'], entry.get('k') or []
    if kind == 0:
        return entry['v']
    node = state
    for key in path[:-1]:
        node = node[key]
    last = path[-1]
    if kind == 1:
        node[last] = entry.get('v')
    elif kind == 3:
        if isinstance(node, dict):
            node.pop(last, None)
        else:
            node[last] = None
    elif kind == 2:
        arr = (node.get(last) if isinstance(node, dict) else node[last]) or []
        if entry.get('i') is not None:
            del arr[entry['i']:]
        arr.extend(entry.get('v') or [])
        node[last] = arr
    return state


def load_raw(path: Path) -> dict:
    if path.suffix == '.json':
        return json.loads(path.read_text())
    state = None
    with path.open('rb') as fh:
        for line in fh:
            if line.strip():
                state = _apply(state, json.loads(line))
    if state is None:
        raise ValueError(f'{path}: archivo de chat vacío o sin snapshot inicial')
    return state


def _text(value) -> str:
    if isinstance(value, dict):
        value = value.get('value', '')
    return LINK.sub(lambda m: m.group(1) or url2pathname(m.group(2)).split('#')[0], value or '')


def _tool(part) -> Optional[ToolCall]:
    name = part.get('toolId') or 'tool'
    detail = _text(part.get('pastTenseMessage') or part.get('invocationMessage'))
    output = ''
    tsd = part.get('toolSpecificData') or {}
    if tsd.get('kind') == 'terminal':
        detail = (tsd.get('commandLine') or {}).get('original') or detail
        output = (tsd.get('terminalCommandOutput') or {}).get('text') or ''
    elif tsd.get('kind') == 'input':
        result = part.get('resultDetails') or {}
        if isinstance(result, dict):
            output = '\n'.join(o.get('value', '') for o in result.get('output') or [] if isinstance(o, dict) and o.get('isText'))
    elif tsd.get('kind') == 'todoList':
        output = '\n'.join(f"- [{t.get('status')}] {t.get('title')}" for t in tsd.get('todoList') or [])
    return ToolCall(name, detail, output)


def _turn(request: dict) -> Turn:
    parts: list = []
    buf = ''
    for p in request.get('response') or []:
        kind = p.get('kind')
        if kind is None and 'value' in p:
            buf += p['value']
            continue
        if kind == 'markdownContent':
            buf += _text(p.get('content'))
            continue
        if kind == 'inlineReference':
            ref = p.get('inlineReference') or {}
            name = p.get('name') or ref.get('name') or os.path.basename((ref.get('uri') or ref.get('location', {}).get('uri') or ref).get('path', ''))
            buf += f'`{name}`'
            continue
        if kind == 'toolInvocationSerialized':
            tool = _tool(p)
        elif kind == 'textEditGroup':
            tool = ToolCall('edit', (p.get('uri') or {}).get('path', ''))
        else:
            continue
        if buf:
            parts.append(buf)
            buf = ''
        if not (parts and isinstance(parts[-1], ToolCall) and parts[-1] == tool):
            parts.append(tool)
    if buf:
        parts.append(buf)
    return Turn(user=(request.get('message') or {}).get('text', ''), timestamp=request.get('timestamp'), parts=parts)


def find(session_id: str) -> dict:
    if os.path.isfile(session_id):
        return _meta_for_path(Path(session_id))
    matches = [s for s in list_sessions() if s['id'].startswith(session_id)]
    if matches and len({s['id'] for s in matches}) == 1:
        # the same session can be copied in several workspaces; use the most recent copy
        return max(matches, key=lambda s: s['updated'])
    raise SystemExit(f'copilot: {len(matches)} sesiones coinciden con "{session_id}"')


def _meta_for_path(path: Path) -> dict:
    ws_dir = path.parent.parent if path.parent.name == 'chatSessions' else None
    entry = _index(ws_dir).get(path.stem, {}) if ws_dir else {}
    return {'source': 'copilot', 'id': path.stem, 'title': entry.get('title') or '(sin título)',
            'cwd': _workspace_cwd(ws_dir) if ws_dir else None,
            'updated': entry.get('lastMessageDate') or int(path.stat().st_mtime * 1000),
            'size': path.stat().st_size, 'path': str(path)}


def active(ws_dir: Path, participant: str, since_ms: int) -> Optional[dict]:
    """Session in ws_dir whose last request was sent to `participant` at or after since_ms."""
    files = sorted((ws_dir / 'chatSessions').glob('*.jsonl'), key=lambda f: f.stat().st_mtime, reverse=True)
    for f in files:
        if f.stat().st_mtime * 1000 < since_ms:
            break
        requests = [r for r in load_raw(f).get('requests') or [] if r]
        last = requests[-1] if requests else {}
        if (last.get('agent') or {}).get('id') == participant and (last.get('timestamp') or 0) >= since_ms:
            return _meta_for_path(f)
    return None


def read(session_id: str, skip_participant: Optional[str] = None) -> Conversation:
    meta = find(session_id)
    raw = load_raw(Path(meta['path']))
    turns = [_turn(r) for r in raw.get('requests') or []
             if r and not (skip_participant and (r.get('agent') or {}).get('id') == skip_participant)]
    title = raw.get('customTitle') or meta['title']
    return Conversation('copilot', meta['id'], title, meta['cwd'], raw.get('creationDate'), turns)


def to_import_json(conv: Conversation, tool_output: Optional[int]) -> dict:
    requests = []
    for turn in conv.turns:
        text = turn.user
        requests.append({
            'requestId': f'request_{uuid.uuid4()}',
            'timestamp': turn.timestamp,
            'message': {'text': text, 'parts': [{
                'range': {'start': 0, 'endExclusive': len(text)},
                'editorRange': {'startLineNumber': 1, 'startColumn': 1,
                                'endLineNumber': text.count('\n') + 1, 'endColumn': len(text.rsplit('\n', 1)[-1]) + 1},
                'text': text, 'kind': 'text'}]},
            'variableData': {'variables': []},
            'response': [{'value': render_assistant(turn, tool_output) or '_(sin respuesta)_',
                          'supportThemeIcons': False, 'supportHtml': False}],
        })
    return {'responderUsername': 'GitHub Copilot', 'initialLocation': 'panel', 'requests': requests}

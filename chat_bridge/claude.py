from __future__ import annotations

import json
import re
import subprocess
import uuid
from pathlib import Path
from typing import List, Optional

from .model import Conversation, ToolCall, Turn, iso_to_ms, ms_to_iso, now_ms, render_assistant
from .paths import claude_home, find_binary

PROJECTS = claude_home() / 'projects'
TITLE_TYPES = ('custom-title', 'ai-title', 'summary')
SKIP_USER_PREFIXES = ('<command-name>', '<command-message>', '<local-command-', 'Caveat:', '[Request interrupted')
DETAIL_KEYS = ('command', 'file_path', 'path', 'pattern', 'url', 'query', 'prompt', 'description')


def binary() -> Optional[str]:
    return find_binary('claude', 'CHAT_BRIDGE_CLAUDE_BIN', 'anthropic.claude-code-*/resources/native-binary')


def version() -> str:
    exe = binary()
    if not exe:
        raise SystemExit('claude: no se encontró el binario de Claude Code (instala Claude Code o define CHAT_BRIDGE_CLAUDE_BIN)')
    out = subprocess.run([exe, '--version'], capture_output=True, text=True, check=True).stdout
    return out.split()[0]


def project_dir(cwd: str) -> Path:
    return PROJECTS / re.sub(r'[^a-zA-Z0-9]', '-', cwd)


def _title(entry: dict) -> Optional[str]:
    return entry.get('customTitle') or entry.get('aiTitle') or entry.get('summary')


def list_sessions() -> List[dict]:
    out = []
    for f in PROJECTS.glob('*/*.jsonl'):
        cwd = title = first_user = None
        with f.open('rb') as fh:
            for line in fh:
                if cwd is None and b'"cwd"' in line:
                    cwd = json.loads(line).get('cwd')
                if any(f'"type":"{t}"'.encode() in line for t in TITLE_TYPES):
                    title = _title(json.loads(line)) or title
                if first_user is None and b'"type":"user"' in line and b'"tool_result"' not in line:
                    content = (json.loads(line).get('message') or {}).get('content')
                    text = content if isinstance(content, str) else next((c.get('text') for c in content or [] if c.get('type') == 'text'), None)
                    if text and not text.startswith(SKIP_USER_PREFIXES):
                        first_user = text
        if cwd is None and first_user is None:
            continue
        out.append({'source': 'claude', 'id': f.stem, 'title': title or (first_user or '(sin título)')[:80],
                    'cwd': cwd, 'updated': int(f.stat().st_mtime * 1000), 'size': f.stat().st_size, 'path': str(f)})
    return out


def find(session_id: str) -> dict:
    matches = [s for s in list_sessions() if s['id'].startswith(session_id)]
    if len(matches) != 1:
        raise SystemExit(f'claude: {len(matches)} sesiones coinciden con "{session_id}"')
    return matches[0]


def _detail(data) -> str:
    if isinstance(data, dict):
        for key in DETAIL_KEYS:
            if data.get(key):
                return str(data[key])
    return json.dumps(data, ensure_ascii=False)


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    return '\n'.join(c.get('text', '') for c in content or [] if isinstance(c, dict) and c.get('type') == 'text')


def read(session_id: str) -> Conversation:
    meta = find(session_id)
    turns: List[Turn] = []
    tools = {}
    created = None
    with open(meta['path'], 'rb') as fh:
        for line in fh:
            entry = json.loads(line)
            if entry.get('type') not in ('user', 'assistant') or entry.get('isSidechain') or entry.get('isMeta') or entry.get('isCompactSummary'):
                continue
            ts = iso_to_ms(entry['timestamp']) if entry.get('timestamp') else None
            created = created or ts
            content = (entry.get('message') or {}).get('content')
            blocks = [{'type': 'text', 'text': content}] if isinstance(content, str) else content or []
            if entry['type'] == 'user':
                texts = []
                for b in blocks:
                    if b.get('type') == 'tool_result' and b.get('tool_use_id') in tools:
                        tools[b['tool_use_id']].output = _result_text(b.get('content'))
                    elif b.get('type') == 'text' and not b.get('text', '').startswith(SKIP_USER_PREFIXES):
                        texts.append(b['text'])
                    elif b.get('type') == 'image':
                        texts.append('[imagen adjunta]')
                if texts:
                    turns.append(Turn(user='\n\n'.join(texts), timestamp=ts))
                continue
            if not turns:
                turns.append(Turn(user='', timestamp=ts))
            for b in blocks:
                if b.get('type') == 'text':
                    turns[-1].parts.append(b['text'])
                elif b.get('type') == 'tool_use':
                    tool = ToolCall(b.get('name', 'tool'), _detail(b.get('input')))
                    tools[b.get('id')] = tool
                    turns[-1].parts.append(tool)
    return Conversation('claude', meta['id'], meta['title'], meta['cwd'], created, turns)


def write(conv: Conversation, cwd: str, tool_output: Optional[int], title: str) -> str:
    session_id = str(uuid.uuid4())
    target = project_dir(cwd)
    target.mkdir(parents=True, exist_ok=True)
    common = {'isSidechain': False, 'userType': 'external', 'cwd': cwd, 'sessionId': session_id, 'version': version()}
    lines = [{'type': 'custom-title', 'customTitle': title, 'sessionId': session_id}]
    parent = None
    base = now_ms()
    for n, turn in enumerate(conv.turns):
        ts = turn.timestamp or base + n
        user_uuid, assistant_uuid = str(uuid.uuid4()), str(uuid.uuid4())
        lines.append({'parentUuid': parent, **common, 'type': 'user',
                      'message': {'role': 'user', 'content': turn.user.strip() or '(mensaje sin texto)'},
                      'uuid': user_uuid, 'timestamp': ms_to_iso(ts)})
        lines.append({'parentUuid': user_uuid, **common, 'type': 'assistant',
                      'message': {'id': f'msg_import_{uuid.uuid4().hex}', 'type': 'message', 'role': 'assistant',
                                  'model': '<synthetic>',
                                  'content': [{'type': 'text', 'text': render_assistant(turn, tool_output) or '(sin respuesta)'}],
                                  'stop_reason': 'end_turn', 'stop_sequence': None,
                                  'usage': {'input_tokens': 0, 'output_tokens': 0,
                                            'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0}},
                      'uuid': assistant_uuid, 'timestamp': ms_to_iso(ts + 1)})
        parent = assistant_uuid
    path = target / f'{session_id}.jsonl'
    with path.open('w', encoding='utf-8') as fh:
        for entry in lines:
            fh.write(json.dumps(entry, ensure_ascii=False) + '\n')
    return session_id

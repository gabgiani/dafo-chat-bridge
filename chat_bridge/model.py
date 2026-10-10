from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Union

ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')


@dataclass
class ToolCall:
    name: str
    detail: str = ''
    output: str = ''


@dataclass
class Turn:
    user: str
    timestamp: Optional[int] = None  # epoch ms
    parts: List[Union[str, ToolCall]] = field(default_factory=list)


@dataclass
class Conversation:
    source: str
    id: str
    title: str
    cwd: Optional[str]
    created: Optional[int]
    turns: List[Turn]


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def iso_to_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() * 1000)


def uuid7(ms: Optional[int] = None) -> str:
    ms = now_ms() if ms is None else ms
    rand = int.from_bytes(os.urandom(10), 'big')
    value = (ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | ((rand >> 64) & 0xFFF) << 64 | 0b10 << 62 | (rand & ((1 << 62) - 1))
    return str(uuid.UUID(int=value))


def clip(text: str, limit: int) -> str:
    if limit < 0 or len(text) <= limit:
        return text
    return text[:limit] + f'\n… [recortado: {len(text) - limit} caracteres más]'


def render_assistant(turn: Turn, tool_output: Optional[int]) -> str:
    """tool_output: None omits tool calls, -1 keeps full output, N clips output and detail to N chars."""
    chunks = []
    for part in turn.parts:
        if isinstance(part, str):
            if part.strip():
                chunks.append(part.strip())
        elif tool_output is not None:
            detail = clip(ANSI.sub('', part.detail).strip(), tool_output if tool_output >= 0 else -1)
            line = f'`[{part.name}]` {detail}'.rstrip()
            output = ANSI.sub('', part.output).replace('\r\n', '\n').strip()
            if output and tool_output != 0:
                line += '\n```\n' + clip(output, tool_output).replace('```', "'''") + '\n```'
            chunks.append(line)
    return '\n\n'.join(chunks)


def render_markdown(conv: Conversation, tool_output: Optional[int]) -> str:
    out = [f'# {conv.title}', '', f'- Origen: {conv.source} `{conv.id}`']
    if conv.cwd:
        out.append(f'- Directorio: `{conv.cwd}`')
    out.append(f'- Turnos: {len(conv.turns)}')
    for turn in conv.turns:
        out += ['', '## Usuario', '', turn.user.strip(), '', '## Asistente', '', render_assistant(turn, tool_output) or '_(sin respuesta)_']
    return '\n'.join(out) + '\n'


def limit_turns(conv: Conversation, last: Optional[int]) -> Conversation:
    if not last or last >= len(conv.turns):
        return conv
    return Conversation(conv.source, conv.id, conv.title, conv.cwd, conv.created, conv.turns[-last:])

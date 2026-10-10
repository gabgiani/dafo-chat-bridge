"""Locations of the agents' data, resolved per platform. Each one can be overridden by an environment variable."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional

VSCODE_PRODUCTS = ('Code', 'Code - Insiders')


def _env_paths(name: str) -> Optional[List[Path]]:
    value = os.environ.get(name)
    return [Path(p).expanduser() for p in value.split(os.pathsep) if p] if value else None


def _app_data() -> Path:
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support'
    if sys.platform == 'win32':
        return Path(os.environ['APPDATA'])
    return Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')


def vscode_user_dirs() -> List[Path]:
    """VS Code `User` folders (CHAT_BRIDGE_VSCODE_USER_DIR, separated by os.pathsep)."""
    explicit = _env_paths('CHAT_BRIDGE_VSCODE_USER_DIR')
    if explicit is not None:
        return explicit
    return [d for d in (_app_data() / p / 'User' for p in VSCODE_PRODUCTS) if d.is_dir()]


def vscode_extension_dirs() -> List[Path]:
    explicit = _env_paths('VSCODE_EXTENSIONS')
    if explicit is not None:
        return explicit
    return [d for d in (Path.home() / '.vscode' / 'extensions', Path.home() / '.vscode-insiders' / 'extensions') if d.is_dir()]


def claude_home() -> Path:
    return Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude').expanduser()


def codex_home() -> Path:
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser()


def chat_bridge_home() -> Path:
    return Path(os.environ.get('CHAT_BRIDGE_HOME') or Path.home() / '.chat-bridge').expanduser()


def find_binary(name: str, env_var: str, extension_glob: str) -> Optional[str]:
    """Binary from env_var, then PATH, then the newest copy bundled in a VS Code extension."""
    if os.environ.get(env_var):
        return os.environ[env_var]
    found = shutil.which(name)
    if found:
        return found
    exe = name + '.exe' if sys.platform == 'win32' else name
    candidates = sorted((c for d in vscode_extension_dirs() for c in d.glob(f'{extension_glob}/{exe}') if c.is_file()),
                        key=lambda c: c.stat().st_mtime)
    return str(candidates[-1]) if candidates else None

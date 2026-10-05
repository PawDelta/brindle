"""Leftovers from brindle's previous name, copse.

brindle never reads them: permission rules, policy and config saved under the
old folders are not applied, and nothing is moved for you. This module only
finds what was left behind and says what to do about it, so a saved deny rule
doesn't stop applying without anyone noticing. It is the one place in brindle
that names copse.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

OLD = "copse"
WARNED_FILE = "legacy-warned.json"


def _agy_settings() -> Path:
    return Path.home() / ".gemini" / "antigravity-cli" / "settings.json"


def _old_keys(path: Path) -> list[str]:
    """Top-level keys, and keys under ``mcpServers``, named copse in a JSON file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    keys = [k for k in data if k == OLD]
    servers = data.get("mcpServers")
    if isinstance(servers, dict) and OLD in servers:
        keys.append(f"mcpServers.{OLD}")
    return keys


def findings(repo_root: str | None) -> list[str]:
    """One line per leftover, each saying what isn't applied and what to do."""
    from brindle.config import brindle_home

    found: list[str] = []
    old_home = Path.home() / f".{OLD}"
    if old_home.exists():
        found.append(f"{old_home} is not read: its saved permission rules, profiles and settings "
                     f"don't apply. Move what you want to keep to {brindle_home()}.")
        if _agy_settings().exists():
            found.append(f"{_agy_settings()} may still hold allow rules that {OLD} mirrored there; "
                         "brindle doesn't manage them, so review its allow list and remove any you don't want.")
    if repo_root:
        root = Path(repo_root)
        old_dir = root / f".{OLD}"
        if old_dir.exists():
            found.append(f"{old_dir} is not read: its config, permission policy and goals don't apply. "
                         f"Move it to {root / '.brindle'}.")
        for name in ("hooks.json", "mcp_config.json"):
            path = root / ".agents" / name
            for key in _old_keys(path):
                found.append(f"{path} still has a '{key}' entry that runs a command that no longer "
                             "exists; remove it.")
    return found


def _warned_path() -> Path:
    from brindle.config import brindle_home

    return brindle_home() / WARNED_FILE


def warn_once(repo_root: str | None) -> list[str]:
    """Print each leftover to stderr the first time it's seen on this machine.

    Returns the lines printed. stdout is left alone: `brindle mcp` and hooks
    speak a protocol there."""
    new = []
    path = _warned_path()
    try:
        seen = set(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        seen = set()
    for line in findings(repo_root):
        if line not in seen:
            new.append(line)
            seen.add(line)
    if not new:
        return []
    print("brindle was renamed from copse; these leftovers are not used "
          "(run `brindle doctor` to see them again):", file=sys.stderr)
    for line in new:
        print(f"  - {line}", file=sys.stderr)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(seen)), encoding="utf-8")
    except OSError:
        pass
    return new

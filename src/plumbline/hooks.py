"""Generate Claude Code hook settings.

Uses the exec form (`command` + `args`) with the absolute interpreter path, so no shell
quoting is involved: a path like C:\\Users\\mayank kumar singh\\... cannot be split on spaces.
Requires `pip install -e .` into that interpreter so `-m plumbline` resolves.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

GUARDED_TOOLS = "Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell"


def _hook(sub: str) -> dict:
    return {"type": "command", "command": sys.executable, "args": ["-m", "plumbline", sub]}


def settings() -> dict:
    return {
        "hooks": {
            "SessionStart": [{"hooks": [_hook("context")]}],
            "PreToolUse": [{"matcher": GUARDED_TOOLS, "hooks": [_hook("guard")]}],
        }
    }


def _is_ours(group: dict) -> bool:
    return any(h.get("args", [])[:2] == ["-m", "plumbline"] for h in group.get("hooks", []))


def merge(existing: dict) -> dict:
    """Idempotent: replaces our own hook groups, leaves everyone else's untouched."""
    out = dict(existing)
    hooks = {k: list(v) for k, v in out.get("hooks", {}).items()}
    for event, groups in settings()["hooks"].items():
        hooks[event] = [g for g in hooks.get(event, []) if not _is_ours(g)] + groups
    out["hooks"] = hooks
    return out


def emit(project_dir: Path, write: bool) -> int:
    if not write:
        print(json.dumps(settings(), indent=2))
        return 0
    path = project_dir / ".claude" / "settings.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merge(existing), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}")
    return 0

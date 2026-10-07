"""PreToolUse guard: makes the memory gate unskippable for file tools.

Claude Code sends the pending tool call as JSON on stdin. Exit code 2 blocks it and
shows stderr to the agent. Direct writes to the gated files are blocked so the only way
in is `plumbline remember` / `plumbline glossary add`, which run the classifier.

Enforcement strength:
  * Write/Edit/MultiEdit/NotebookEdit: strict, by resolved file path.
  * Bash/PowerShell: best-effort heuristic (a shell can always be obfuscated), so it
    blocks commands that mention a gated file unless they look read-only.
Malformed input fails OPEN: a broken guard must not brick the session.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import PureWindowsPath

PROTECTED = ("memory.json", "glossary.json", "ignores.json", "key")  # key: also unreadable (see decide)
FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}

_MENTION = re.compile(r"\.plumbline[\\/]+(?:memory|glossary|ignores)\.json", re.I)
_KEY = re.compile(r"\.plumbline[\\/]+key\b", re.I)  # the signing key is never readable or writable
_READONLY = re.compile(r"^\s*(?:cat|type|Get-Content|gc|head|tail|less|more|grep|rg|ls|dir)\b", re.I)
_REDIRECT = re.compile(r"(?<![<-])>{1,2}|\|\s*(?:tee|Out-File|Set-Content)\b", re.I)

MESSAGE = (
    "Blocked: {what} is gated memory. Write to it only through the CLI so the classifier runs:\n"
    '  plumbline remember "<durable fact>"      (facts)\n'
    "  plumbline glossary add <container> --meaning \"...\"   (glossary)\n"
    "Volatile state (running containers, current branch) must be queried live, not stored."
)


def _is_protected_path(path: str) -> bool:
    parts = PureWindowsPath(path.replace("/", "\\")).parts
    return len(parts) >= 2 and parts[-2].lower() == ".plumbline" and parts[-1].lower() in PROTECTED


def decide(payload: dict) -> str | None:
    """Return a block message, or None to allow."""
    tool = payload.get("tool_name", "")
    inp = payload.get("tool_input") or {}
    if tool == "Read":
        path = inp.get("file_path") or ""
        return MESSAGE.format(what=path) if path and _is_protected_path(path) and path.lower().endswith("key") else None
    if tool in FILE_TOOLS:
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        if path and _is_protected_path(path):
            return MESSAGE.format(what=path)
    elif tool in SHELL_TOOLS:
        cmd = inp.get("command") or ""
        if _KEY.search(cmd) or (_MENTION.search(cmd) and (_REDIRECT.search(cmd) or not _READONLY.match(cmd))):
            return MESSAGE.format(what="a file under .plumbline/")
    return None


def run(stdin_text: str) -> int:
    try:
        payload = json.loads(stdin_text)
    except (json.JSONDecodeError, TypeError):
        _log("guard_failopen", reason="unparseable stdin")
        return 0
    if not isinstance(payload, dict):
        _log("guard_failopen", reason="payload is not an object")
        return 0
    msg = decide(payload)
    if msg:
        _log("guard_block", tool=payload.get("tool_name"), cwd=payload.get("cwd"))
        print(msg, file=sys.stderr)
        return 2
    return 0


def _log(type_: str, **fields) -> None:
    """Record guard activity so a silently inert guard shows up in the metrics."""
    try:
        from .store import Workspace
        Workspace.find().log_event(type_, **fields)
    except Exception:  # noqa: BLE001 - logging must never change the guard's verdict
        pass

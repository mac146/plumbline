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
from pathlib import Path, PureWindowsPath

PROTECTED = ("memory.json", "glossary.json", "ignores.json", "quarantine.json", "key")  # key: also unreadable (see decide)
FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}

_MENTION = re.compile(r"\.plumbline[\\/]+(?:memory|glossary|ignores|quarantine)\.json", re.I)
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


_RESOLVE = re.compile(r"\bplumbline\b[^\n;|&]*?\b(?:glossary\s+(?:add|confirm|remove)|flags\s+(?:snooze|ignore)|memory\s+(?:release|forget|confirm))\b", re.I)
_DOCKERISH = re.compile(r"\bdocker(?:-compose)?\b", re.I)
# Anything that changes state. Deliberately broad: it only matters when the command also names a protected target.
_DESTRUCTIVE = re.compile(
    r"\b(?:truncate|delete|drop|dropdb|update|insert|alter|create|grant|revoke|reindex|vacuum|copy|reset"
    r"|restart|stop|start|rm|kill|down|up|run|pause|unpause|rename|prune|mv|dd|mkfs|chmod|chown)\b"
    r"|\bpsql\b[^\n;|&]*?\s-f\s|\s<\s*\S",
    re.I)
# Commands whose scope is "everything" even though they name no container.
_ALL_SCOPE = re.compile(
    r"\$\(\s*docker\b|`\s*docker\b"
    r"|\bdocker\s+(?:system|container|volume|network|image)\s+prune\b|\bdocker\s+volume\s+rm\b"
    r"|(?:docker-compose|docker\s+compose)\b(?:\s+-{1,2}\S+(?:\s+[^\s-]\S*)?)*\s+(?:down|stop|restart|kill|rm|up)\s*(?:-\S+\s*)*(?=$|[;&|])",
    re.I)

_PROTECT_MEANING = re.compile(r"never\s+modify|do\s+not\s+modify|must\s+not|read-?only|do\s+not\s+touch", re.I)

RESOLVE_MESSAGE = (
    "Blocked: confirming, adding or silencing a glossary entry is a decision for the user, not the agent. "
    "Ask the user which container is which, and let them run the plumbline command themselves."
)
DRIFT_MESSAGE = (
    "Blocked: this would act on {names}, which Plumbline reports as DRIFTED (it no longer matches what the "
    "user last confirmed it was). Do not act on it. Ask the user to confirm which container is which, "
    "then they can run `plumbline glossary confirm`."
)


def _normalize(cmd: str) -> str:
    """Strip quoting/line-continuations so 'dock'er', "docker" and docker\\<newline>restart look alike."""
    return re.sub(r"\s+", " ", cmd.replace("\\\n", " ").replace("'", "").replace('"', "").replace("`", "`"))


def _drift_block(cmd: str, protected: set[str]) -> str | None:
    """Block a state-changing docker/compose command that names a protected target or has everything as scope."""
    if not protected:
        return None
    norm = _normalize(cmd)
    if not _DOCKERISH.search(norm) or not _DESTRUCTIVE.search(norm):
        return None
    hit = sorted(n for n in protected if re.search(rf"(?<![\w.-]){re.escape(n)}(?![\w-])", norm, re.I))
    if hit:
        return DRIFT_MESSAGE.format(names=", ".join(hit))
    if _ALL_SCOPE.search(norm):
        return DRIFT_MESSAGE.format(names="all containers (the command has no specific target)")
    return None


def live_flagged_names(cwd: str | None = None) -> set[str]:
    """Container names AND compose services the glossary currently reports as drifted or ambiguous."""
    from .drift import Ignores, evaluate
    from .glossary import Glossary
    from .probes import snapshot
    from .store import Workspace

    try:
        ws = Workspace.find(Path(cwd) if cwd else None)
        state = snapshot(ws.root, [])
        if state.containers is None:
            return set()
        _, flags = evaluate(state.containers, Glossary(ws), Ignores(ws))
    except Exception:  # noqa: BLE001 - never break the session over a probe problem
        return set()
    names: set[str] = set()

    def protect(c) -> None:
        names.add(c.name)
        if c.service:
            names.add(c.service)
        names.update(v for v in c.volumes if v and not v.startswith("/"))  # volume names, not host paths

    for f in flags:
        if f.container is not None and f.kind in ("meaning_changed", "ambiguous"):
            protect(f.container)
    # A container the user labelled "never modify" is protected on its own, whether or not anything drifted.
    gl = Glossary(ws)
    for c in state.containers:
        m = gl.match(c)
        if m and _PROTECT_MEANING.search(m.entry["meaning"]):
            protect(c)
    return names


def decide(payload: dict, flagged_provider=None) -> str | None:
    """Return a block message, or None to allow. `flagged_provider()` returns drifted container/service names."""
    tool = payload.get("tool_name", "")
    inp = payload.get("tool_input") or {}
    if tool in SHELL_TOOLS:
        cmd = inp.get("command") or ""
        if _RESOLVE.search(cmd):
            return RESOLVE_MESSAGE
        if flagged_provider is not None and _DOCKERISH.search(cmd):
            msg = _drift_block(cmd, flagged_provider())
            if msg:
                return msg
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
    msg = decide(payload, lambda: live_flagged_names(payload.get("cwd")))
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

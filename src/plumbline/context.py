"""Session-start context: stable memory + live state resolved through the glossary."""

from __future__ import annotations

from datetime import datetime

from .drift import Ignores, evaluate
from .glossary import Glossary
from .memory import Memory
from .probes import LiveState, snapshot
from .store import Workspace, utcnow


def build(ws: Workspace, state: LiveState | None = None, now: datetime | None = None) -> str:
    now = now or utcnow()
    cfg = ws.config()
    state = state or snapshot(ws.root, cfg["env_vars"])
    mem = Memory(ws)
    fresh, stale = mem.split(now)
    forced = [e for e in fresh if e["kind"] == "forced"]
    fresh = [e for e in fresh if e["kind"] != "forced"]
    unverified = mem.unverified()
    out: list[str] = ["# Plumbline context", ""]

    out += ["## Stable facts (remembered; safe to rely on)"]
    out += [f"- {e['text']}" + (f" (asserted durable: {e['why']})" if e.get("why") else "") for e in fresh] \
        or ["- (none yet)"]
    if forced:
        out += ["", "## Stored by human override (not classified; treat with care)"]
        out += [f"- {e['text']} (reason: {e.get('forced_reason', '?')})" for e in forced]
    if unverified:
        out += ["", "## UNVERIFIED entries: did not come through the gate; NOT trusted, do not rely on them"]
        out += [f"- [{e.get('id', '?')}] {e.get('text', '?')}" for e in unverified]
        out += ["Ask the user to review: `plumbline memory list`, then `forget` or re-add through `plumbline remember`."]
    if stale:
        out += ["", "## Semi-stable facts past their expiry (verify before relying on them)"]
        out += [f"- [{e['id']}] {e['text']}" + (f" (verify with: {e['verify_with']})" if e.get("verify_with") else "")
                for e in stale]

    out += ["", "## Live state (queried just now; do not substitute memory for this)"]
    if state.branch is not None:
        out.append(f"- git: branch `{state.branch}` @ {state.head or '?'}, "
                   f"{state.dirty_files if state.dirty_files is not None else '?'} changed files")
    else:
        out.append(f"- git: unavailable ({state.git_error})")
    for k, v in state.env.items():
        out.append(f"- env {k}={v}")

    flags = []
    if state.containers is None:
        out.append(f"- docker: unavailable ({state.docker_error}); container state UNKNOWN, do not assume.")
    else:
        resolved, flags = evaluate(state.containers, Glossary(ws), Ignores(ws), now)
        out += [f"- container `{r.container.name}` = {r.meaning}" for r in resolved]
        if not state.containers:
            out.append("- docker: no containers running")

    if flags:
        out += ["", "## Needs the user (do NOT guess; ask, then record the answer)"]
        for f in flags:
            out.append(f"- [{f.severity.upper()}] {f.message}  key=`{f.key}`")
        out += ["", "Resolve with `plumbline glossary add <container> --meaning ...`, "
                "`plumbline glossary confirm <id> <container>`, or `plumbline flags snooze|ignore <key>`."]

    for e in unverified:
        ws.log_event("integrity_violation", now=now, id=e.get("id"))
    for f in flags:
        ws.log_event("flag_raised", now=now, key=f.key, severity=f.severity, kind=f.kind)
    ws.log_event("session_start", now=now, flags=len(flags))
    return "\n".join(out) + "\n"

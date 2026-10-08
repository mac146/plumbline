"""Command line interface. `context` never fails (it runs from a session-start hook)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import context as ctx
from .drift import HIGH, Ignores, evaluate
from .glossary import Glossary
from .memory import Memory
from .metrics import summarize
from .probes import snapshot
from .store import Workspace, WorkspaceNotFound


def _live_container(ws: Workspace, ref: str):
    state = snapshot(ws.root, ws.config()["env_vars"])
    if state.containers is None:
        raise SystemExit(f"docker unavailable: {state.docker_error}")
    hits = [c for c in state.containers if ref in (c.name, c.id) or c.id.startswith(ref)]
    if len(hits) != 1:
        raise SystemExit(f"expected exactly one running container matching '{ref}', found {len(hits)}")
    return hits[0]


def _kv(items: list[str] | None) -> dict | None:
    if not items:
        return None
    sel: dict = {}
    for item in items:
        k, _, v = item.partition("=")
        if k == "port":
            sel.setdefault("port", []).append(int(v))
        else:
            sel[k] = v
    return sel


def _flag_for_key(ws: Workspace, key: str):
    state = snapshot(ws.root, ws.config()["env_vars"])
    if state.containers is None:
        return None
    _, flags = evaluate(state.containers, Glossary(ws), Ignores(ws))
    return next((f for f in flags if f.key == key), None)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="plumbline")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create .plumbline/ here")
    sub.add_parser("context", help="print session-start context (use from a SessionStart hook)")

    r = sub.add_parser("remember", help="write a fact through the gate")
    r.add_argument("text")
    r.add_argument("--ttl-days", type=int)
    r.add_argument("--verify-with", help="how to re-verify a semi-stable fact")
    r.add_argument("--type", choices=["config", "convention", "decision"], help="structure: what kind of fact")
    r.add_argument("--why", help="structure: one line on why this won't change")
    r.add_argument("--force", action="store_true", help="human override of the gate (needs --reason)")
    r.add_argument("--reason", help="why the override is justified")

    m = sub.add_parser("memory", help="manage remembered facts")
    msub = m.add_subparsers(dest="mcmd", required=True)
    msub.add_parser("list")
    msub.add_parser("verify", help="list entries that did not come through the gate")
    mc = msub.add_parser("confirm")
    mc.add_argument("id")
    mr = msub.add_parser("release", help="release a quarantined fact after reviewing it (human decision)")
    mr.add_argument("id")
    mf = msub.add_parser("forget")
    mf.add_argument("id")

    g = sub.add_parser("glossary", help="manage the name->meaning glossary")
    gsub = g.add_subparsers(dest="gcmd", required=True)
    gsub.add_parser("list")
    ga = gsub.add_parser("add")
    ga.add_argument("container", help="running container name or id")
    ga.add_argument("--meaning", required=True)
    ga.add_argument("--selector", action="append", help="override, e.g. service=db (repeatable)")
    gc = gsub.add_parser("confirm", help="re-confirm an entry whose meaning drifted")
    gc.add_argument("id")
    gc.add_argument("container")
    gr = gsub.add_parser("remove")
    gr.add_argument("id")

    f = sub.add_parser("flags", help="dismiss drift flags")
    fsub = f.add_subparsers(dest="fcmd", required=True)
    fs = fsub.add_parser("snooze")
    fs.add_argument("key")
    fs.add_argument("--hours", type=float, default=24)
    fi = fsub.add_parser("ignore", help="ignore forever (not allowed for high severity)")
    fi.add_argument("key")

    lg = sub.add_parser("log", help="record a measurement event")
    lg.add_argument("kind", choices=["reexplain", "wrong-target"])
    lg.add_argument("note")

    sub.add_parser("guard", help="PreToolUse hook: block direct writes to gated memory files")
    hk = sub.add_parser("hooks", help="print (or --write) Claude Code hook settings")
    hk.add_argument("--write", action="store_true", help="merge into ./.claude/settings.json")

    mt = sub.add_parser("metrics")
    mt.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "init":
        ws = Workspace(Path.cwd())
        ws.init()
        print(f"initialised {ws.dir}")
        return 0
    if args.cmd == "guard":
        from .guard import run as guard_run
        return guard_run(sys.stdin.read())
    if args.cmd == "hooks":
        from .hooks import emit
        return emit(Path.cwd(), write=args.write)
    try:
        ws = Workspace.find()
    except WorkspaceNotFound as e:
        if args.cmd == "context":
            return 0  # a hook must never break session start
        print(e, file=sys.stderr)
        return 1

    if args.cmd == "context":
        try:
            sys.stdout.write(ctx.build(ws))
        except Exception as e:  # noqa: BLE001 - hook safety net
            print(f"plumbline: context unavailable ({e})", file=sys.stderr)
        return 0

    if args.cmd == "remember":
        live = None if args.force else snapshot(ws.root, ws.config()["env_vars"])
        res = Memory(ws).remember(
            args.text, ttl_days=args.ttl_days, verify_with=args.verify_with, type=args.type,
            why=args.why, force=args.force, reason=args.reason, live=live,
        )
        print(res.message)
        return 0 if res.accepted or "Already remembered" in res.message else 2

    if args.cmd == "memory":
        mem = Memory(ws)
        if args.mcmd == "list":
            fresh, stale = mem.split()
            for e in fresh:
                print(f"{e['id']}  {e['kind']:<11} {e['text']}")
            for e in mem.unverified():
                print(f"{e.get('id', '?')}  UNVERIFIED  {e.get('text', '?')}")
            for e in stale:
                print(f"{e['id']}  EXPIRED     {e['text']}")
        elif args.mcmd == "verify":
            bad = mem.unverified()
            for e in bad:
                print(f"UNVERIFIED {e.get('id', '?')}  {e.get('text', '?')}")
            print(f"{len(bad)} unverified of {len(mem.all())}")
            return 1 if bad else 0
        elif args.mcmd == "confirm":
            mem.confirm(args.id)
            print(f"confirmed {args.id}")
        elif args.mcmd == "release":
            from .quarantine import Ledger
            print(f"released {args.id}" if Ledger(ws).release(args.id) else f"{args.id} was not quarantined")
        else:
            mem.forget(args.id)
            from .quarantine import Ledger
            Ledger(ws).release(args.id)
            print(f"forgot {args.id}")
        return 0

    if args.cmd == "glossary":
        gl = Glossary(ws)
        if args.gcmd == "list":
            for e in gl.all():
                print(f"{e['id']}  {json.dumps(e['selector'])}  = {e['meaning']}")
        elif args.gcmd == "add":
            e = gl.add(_live_container(ws, args.container), args.meaning, _kv(args.selector))
            ws.log_event("flag_resolved", action="add", id=e["id"])
            print(f"added {e['id']}: {json.dumps(e['selector'])} = {e['meaning']}")
        elif args.gcmd == "confirm":
            gl.confirm(args.id, _live_container(ws, args.container))
            ws.log_event("flag_resolved", action="confirm", id=args.id)
            print(f"confirmed {args.id}")
        else:
            gl.remove(args.id)
            print(f"removed {args.id}")
        return 0

    if args.cmd == "flags":
        flag = _flag_for_key(ws, args.key)
        sev = flag.severity if flag else severity_unknown()
        ig = Ignores(ws)
        if args.fcmd == "snooze":
            ig.snooze(args.key, args.hours)
            ws.log_event("flag_resolved", action="snooze", key=args.key)
            print(f"snoozed {args.key} for {args.hours:g}h")
        else:
            try:
                ig.forever(args.key, sev)
            except PermissionError as e:
                print(e, file=sys.stderr)
                return 2
            ws.log_event("flag_resolved", action="ignore", key=args.key)
            print(f"ignored {args.key}")
        return 0

    if args.cmd == "log":
        ws.log_event(args.kind.replace("-", "_"), note=args.note)
        print("logged")
        return 0

    if args.cmd == "metrics":
        s = summarize(ws.read_events())
        if args.json:
            print(json.dumps(s, indent=2))
        else:
            for k, v in s.items():
                print(f"{k}: {v if v is not None else 'n/a'}")
        return 0
    return 1


def severity_unknown() -> str:
    # If the flag is not currently live we cannot prove it is low-risk, so be conservative.
    return HIGH


if __name__ == "__main__":
    raise SystemExit(main())

"""Run the pre-registered A/B (see bench/PREREGISTRATION.md). One fresh sandbox per run.

  python -m bench.run --out bench/results/main.jsonl                       # scored: 4 arms x 4 tasks x 10
  python -m bench.run --runs 1 --label smoke --out bench/results/smoke-2.jsonl
  python -m bench.run --dry-run ...                                        # no claude calls

Scored runs (label 'main') refuse to start unless bench/FROZEN.sha256 verifies, pin the model, run a
preflight call, record provenance in every row, abort after consecutive errors (rate limits must not
silently become 'error' results), and resume from an existing output file.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from bench import freeze
from bench.checks import TASKS, outcome
from bench.questions import ANSWERS_BY_KIND, question_kind
from bench.sandbox import SetupError, Sandbox, build

MAX_TURNS = 15
ALLOWED = "Bash,Read,Edit,Write,Glob,Grep"
DEFAULT_ARMS = "A,B,Bclean,C"
PINNED_MODEL = "claude-sonnet-4-6"
MAX_CONSECUTIVE_ERRORS = 5
ROOT = Path(__file__).resolve().parents[1]


def parse_stream(stdout: str) -> dict:
    result, model = {}, None
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            model = ev.get("model")
        if ev.get("type") == "result":
            result = ev
    return {
        "text": result.get("result", "") or "",
        "session_id": result.get("session_id"),
        "turns": result.get("num_turns", 0),
        "cost": result.get("total_cost_usd", 0.0) or 0.0,
        "is_error": bool(result.get("is_error")) or not result,
        "model": model,
    }


def claude_call(prompt: str, sb: Sandbox, *, resume: str | None, model: str | None, budget: float,
                timeout: int) -> tuple[dict, str]:
    cmd = [shutil.which("claude") or "claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--max-turns", str(MAX_TURNS), "--allowedTools", ALLOWED, "--permission-mode", "acceptEdits",
           "--setting-sources", "project,local", "--strict-mcp-config", "--disable-slash-commands",
           "--max-budget-usd", str(budget)]
    if model:
        cmd += ["--model", model]
    if resume:
        cmd += ["--resume", resume]
    try:
        p = subprocess.run(cmd, cwd=sb.repo, env={**os.environ, **sb.env}, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        return parse_stream(p.stdout), p.stdout
    except subprocess.TimeoutExpired as e:
        return {"text": "", "session_id": None, "turns": 0, "cost": 0.0, "is_error": True, "model": None,
                "timeout": True}, (e.stdout or "") if isinstance(e.stdout, str) else ""


def _sh(*cmd: str) -> str:
    try:
        return subprocess.run(list(cmd), capture_output=True, text=True, cwd=ROOT).stdout.strip()
    except OSError:
        return ""


def provenance() -> dict:
    return {
        "claude_version": _sh(shutil.which("claude") or "claude", "--version"),
        "git_commit": _sh("git", "rev-parse", "HEAD"),
        "git_dirty": bool(_sh("git", "status", "--porcelain", "--", "bench", "src", ":(exclude)bench/results")),
        "freeze_hash": freeze.combined() if freeze.FROZEN.exists() else None,
        "pinned_model": PINNED_MODEL,
    }


def one_run(arm: str, task: str, i: int, args, out_dir: Path, prov: dict) -> dict:
    spec = TASKS[task]
    row = {"arm": arm, "task": task, "run": i, "label": args.label, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
           **prov}
    root = Path(tempfile.mkdtemp(prefix="plbench-")).resolve()  # long path: 8.3 names trip a file-tool approval check
    try:
        sb = build(arm, task, root)
    except SetupError as e:
        shutil.rmtree(root, ignore_errors=True)
        return {**row, "outcome": "error", "outcome_before_answer": "error", "error": f"setup: {e}"}
    transcript = ""
    try:
        if args.dry_run:
            res = outcome(task, sb.repo, sb.world_dir, sb.initial_branches)
            row.update(outcome=res, outcome_before_answer=res, asked_question=False, asked_kind="none",
                       reexplain=False, turns=0, cost_usd=0.0, model=None, final_text="(dry run)")
            return row
        first, raw = claude_call(spec["prompt"], sb, resume=None, model=args.model, budget=args.budget,
                                 timeout=args.timeout)
        transcript = raw
        before = outcome(task, sb.repo, sb.world_dir, sb.initial_branches)
        kind = question_kind(first["text"]) if before == "no_action" and not first["is_error"] else "none"
        turns, cost, text, res = first["turns"], first["cost"], first["text"], before
        if kind != "none" and first["session_id"]:
            answer = spec["answer"] if kind == "target" else ANSWERS_BY_KIND[kind]
            second, raw2 = claude_call(answer, sb, resume=first["session_id"], model=args.model,
                                       budget=args.budget, timeout=args.timeout)
            transcript += "\n" + raw2
            turns += second["turns"]
            cost += second["cost"]
            text = second["text"] or text
            res = outcome(task, sb.repo, sb.world_dir, sb.initial_branches)
        errored = first["is_error"] and before == "no_action"
        row.update(
            outcome="error" if errored else res,
            # The real signal: what the arm did BEFORE the scripted user handed it any ground truth.
            outcome_before_answer="error" if errored else before,
            asked_question=kind != "none", asked_kind=kind,
            reexplain=bool(kind == "target" and arm != "A" and spec["reexplain_scored"]),
            first_text=first["text"][:1500], turns=turns, cost_usd=round(cost, 4), model=first["model"],
            final_text=text[:2000],
        )
        return row
    finally:
        if transcript:
            tdir = out_dir / "transcripts"
            tdir.mkdir(parents=True, exist_ok=True)
            (tdir / f"{arm}-{task}-{i}-{int(time.time())}.jsonl").write_text(transcript, encoding="utf-8")
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)


def make_plan(arms: list[str], tasks: list[str], runs: int, seed: int) -> list[tuple[str, str, int]]:
    """Round-robin by run index; within each round every (task, arm) cell appears once in random order,
    so arms are interleaved in time and a partial batch is still balanced."""
    rng, plan = random.Random(seed), []
    for i in range(runs):
        cells = [(a, t, i) for t in tasks for a in arms]
        rng.shuffle(cells)
        plan += cells
    return plan


def _done(path: Path, label: str) -> set:
    if not path.exists():
        return set()
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    return {(r["arm"], r["task"], r["run"]) for r in rows if r.get("label") == label}


def preflight(args) -> str | None:
    """One tiny call to prove the pinned model and CLI work before spending 160 runs."""
    root = Path(tempfile.mkdtemp(prefix="plbench-pre-")).resolve()
    try:
        sb = build("A", "T4", root)
        res, _ = claude_call("Reply with the single word: ok", sb, resume=None, model=args.model, budget=0.25,
                             timeout=120)
        if res["is_error"]:
            return "preflight call failed"
        if args.model and res["model"] and args.model not in res["model"]:
            return f"requested model {args.model} but got {res['model']}"
        return None
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bench.run")
    ap.add_argument("--arms", default=DEFAULT_ARMS)
    ap.add_argument("--tasks", default="T1,T2,T3,T4")
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--label", default="main", help="'main' for scored runs; 'smoke' for harness checks")
    ap.add_argument("--model", default=PINNED_MODEL)
    ap.add_argument("--budget", type=float, default=1.0, help="max USD per claude call")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--seed", type=int, default=20261007)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if args.label == "main" and not args.dry_run:
        ok, problems = freeze.verify()
        if not ok:
            print("refusing to run scored runs:\n  " + "\n  ".join(problems), file=sys.stderr)
            return 2
        bad = preflight(args)
        if bad:
            print(f"refusing to run: {bad}", file=sys.stderr)
            return 2
    prov = provenance()
    plan = make_plan(args.arms.split(","), args.tasks.split(","), args.runs, args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    done = _done(args.out, args.label)
    spent, errors_in_a_row = 0.0, 0
    for n, (arm, task, i) in enumerate(plan, 1):
        if (arm, task, i) in done:
            continue
        row = one_run(arm, task, i, args, args.out.parent, prov)
        spent += row.get("cost_usd", 0.0)
        with args.out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        print(f"[{n}/{len(plan)}] {arm} {task}#{i}: {row['outcome']}"
              f"{' (asked ' + row['asked_kind'] + ')' if row.get('asked_question') else ''} "
              f"turns={row.get('turns')} spent=${spent:.2f}", flush=True)
        errors_in_a_row = errors_in_a_row + 1 if row["outcome"] == "error" else 0
        if errors_in_a_row >= MAX_CONSECUTIVE_ERRORS and not args.dry_run:
            print(f"aborting after {errors_in_a_row} consecutive errors (rate limit / outage?). "
                  "Re-run the same command to resume.", file=sys.stderr)
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())

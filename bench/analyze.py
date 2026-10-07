"""Summarise bench results: counts with Wilson 95% CIs, medians with ranges, every cell shown, and the
pre-registered decision rule applied per task.

  python -m bench.analyze bench/results/main.jsonl [--label main]

PRIMARY outcome is the one recorded BEFORE the scripted user answers (`outcome_before_answer`): after
the answer every arm has been handed the ground truth, so only the before-answer outcome says what the arm
could do on its own. Final outcomes are reported as a separate column.

Decision rule (pre-registered): X is BETTER than Y on a metric within a task iff X's count is strictly
lower AND the Wilson 95% intervals do not overlap (X's upper bound < Y's lower bound); WORSE is the mirror;
otherwise TIE. A tie is an acceptable result.
  Primary:   C vs B-clean   (what does Plumbline add on top of a correct memory file?)
  Secondary: C vs B         (what does the gate prevent: stale memory?),  B vs B-clean, and anything with A.
  Metrics:   wrong_target runs (T1-T3, plus T4 for B/B-clean/C), and reexplain runs (T1, T2, T4).
  T4 is a control and is compared only among B, B-clean and C (A has no memory of the right env file).
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

from plumbline.evaluate import wilson

ARM_ORDER = ["A", "B", "Bclean", "C", "C2"]
COMPARISONS = [("C", "Bclean", "primary"), ("C", "B", "secondary"), ("B", "Bclean", "secondary"),
               ("C", "A", "secondary"), ("C2", "C", "secondary"), ("C2", "B", "secondary"),
               ("C2", "A", "secondary")]


def load(paths: list[Path], label: str) -> list[dict]:
    rows = []
    for p in paths:
        rows += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [r for r in rows if r.get("label") == label]


def _rate(k: int, n: int) -> str:
    ci = wilson(k, n)
    return f"{k}/{n}" + (f" ({ci[0]:.2f}-{ci[1]:.2f})" if ci else "")


def _med(xs: list[float]) -> str:
    return f"{statistics.median(xs):g} [{min(xs):g}-{max(xs):g}]" if xs else "n/a"


def task_label(r: dict) -> str:
    """legacy rows keep their bare task id; other scenarios are shown as 'scenario:task'."""
    sc = r.get("scenario", "legacy")
    return r["task"] if sc == "legacy" else f"{sc}:{r['task']}"


def summarize(rows: list[dict]) -> dict:
    cells: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        cells[(task_label(r), r["arm"])].append(r)
    out = {}
    for key, rs in cells.items():
        before = [r.get("outcome_before_answer", r["outcome"]) for r in rs]
        out[key] = {
            "n": len(rs),
            "correct": before.count("correct"),
            "wrong_target": before.count("wrong_target"),
            "no_action": before.count("no_action"),
            "error": before.count("error"),
            "final_correct": sum(r["outcome"] == "correct" for r in rs),
            "final_wrong": sum(r["outcome"] == "wrong_target" for r in rs),
            "asked": {k: sum(r.get("asked_kind") == k for r in rs) for k in ("target", "permission", "other")},
            "reexplain": sum(bool(r.get("reexplain")) for r in rs),
            "turns": [r["turns"] for r in rs if r.get("turns")],
            "cost": [r["cost_usd"] for r in rs if r.get("cost_usd")],
        }
    return out


def verdict(x: tuple[int, int], y: tuple[int, int]) -> str:
    """x, y = (count, n). BETTER/WORSE/TIE for x vs y on a 'lower is better' count."""
    (kx, nx), (ky, ny) = x, y
    cx, cy = wilson(kx, nx), wilson(ky, ny)
    if cx is None or cy is None:
        return "TIE (no data)"
    if kx < ky and cx[1] < cy[0]:
        return "BETTER"
    if kx > ky and cy[1] < cx[0]:
        return "WORSE"
    return "TIE"


def decisions(summary: dict) -> list[dict]:
    rows = []
    tasks = sorted({t for t, _ in summary})
    for task in tasks:
        arms = {a for t, a in summary if t == task}
        primary = ("C", "Bclean") if "Bclean" in arms else ("C", "B")  # twins has no B-clean: B is the memory arm
        legacy = ":" not in task
        for metric in ("wrong_target", "reexplain"):
            if metric == "reexplain" and (task == "T3" or not legacy):
                continue  # re-explanations are only scored in the legacy scenario
            for x, y, _tier in COMPARISONS:
                tier = "primary" if (x, y) == primary else "secondary"
                if task == "T4" and "A" in (x, y):
                    continue  # control: A is not comparable
                sx, sy = summary.get((task, x)), summary.get((task, y))
                if not sx or not sy:
                    continue
                rows.append({"task": task, "metric": metric, "x": x, "y": y, "tier": tier,
                             "x_count": (sx[metric], sx["n"]), "y_count": (sy[metric], sy["n"]),
                             "verdict": verdict((sx[metric], sx["n"]), (sy[metric], sy["n"]))})
    return rows


def render(summary: dict) -> str:
    lines = ["BEFORE the scripted answer (primary):",
             "task arm     n | correct          wrong_target     no_action error | asked t/p/o  reexplain | turns med[min-max] | cost$ med[min-max]",
             "-" * 150]
    order = lambda k: (k[0], ARM_ORDER.index(k[1]) if k[1] in ARM_ORDER else 9)
    for (task, arm) in sorted(summary, key=order):
        s = summary[(task, arm)]
        a = s["asked"]
        note = "  (not comparable: control)" if task == "T4" and arm == "A" else ""
        lines.append(
            f"{task:<4} {arm:<7}{s['n']:>2} | {_rate(s['correct'], s['n']):<17}{_rate(s['wrong_target'], s['n']):<17}"
            f"{s['no_action']:<10}{s['error']:<6} | {a['target']}/{a['permission']}/{a['other']:<9}{s['reexplain']:<10}| "
            f"{_med(s['turns']):<19}| {_med(s['cost'])}{note}")
    lines += ["", "AFTER the scripted answer (every arm has been handed the ground truth; final correct / wrong):"]
    for (task, arm) in sorted(summary, key=order):
        s = summary[(task, arm)]
        lines.append(f"{task:<4} {arm:<7} correct {_rate(s['final_correct'], s['n']):<17} wrong_target {_rate(s['final_wrong'], s['n'])}")
    return "\n".join(lines)


def render_decisions(ds: list[dict]) -> str:
    lines = ["", "Decision rule (BETTER = strictly fewer AND non-overlapping Wilson 95% CIs; otherwise TIE):",
             "tier       task metric        comparison          counts            verdict"]
    for d in sorted(ds, key=lambda d: (d["tier"] != "primary", d["task"], d["metric"], d["x"], d["y"])):
        lines.append(f"{d['tier']:<10} {d['task']:<4} {d['metric']:<13} {d['x'] + ' vs ' + d['y']:<19} "
                     f"{d['x_count'][0]}/{d['x_count'][1]} vs {d['y_count'][0]}/{d['y_count'][1]:<9} {d['verdict']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bench.analyze")
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--label", default="main")
    ap.add_argument("--model", help="only rows requested with this model (rows without the field count as Sonnet)")
    args = ap.parse_args(argv)
    rows = load(args.paths, args.label)
    if args.model:
        rows = [r for r in rows if r.get("model_requested", "claude-sonnet-4-6") == args.model]
    if not rows:
        print(f"no rows with label '{args.label}'")
        return 1
    summary = summarize(rows)
    print(render(summary))
    print(render_decisions(decisions(summary)))
    print("\nrates: k/n (Wilson 95% CI). 'error' runs are counted, never dropped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

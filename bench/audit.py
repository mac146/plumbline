"""Audit the question heuristic: sample runs that asked something, hand-label them, measure agreement.

  python -m bench.audit sample bench/results/main.jsonl --n 20 --out bench/results/question_audit.jsonl
  (fill in "human": target | permission | other for each row, looking only at "first_text")
  python -m bench.audit score bench/results/question_audit.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from bench.questions import question_kind


def sample(paths: list[Path], n: int, out: Path) -> int:
    rows = []
    for p in paths:
        rows += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    asked = [r for r in rows if r.get("label") == "main" and r.get("asked_kind", "none") != "none"]
    random.Random(1).shuffle(asked)
    picked = asked[:n]
    # The heuristic's label is deliberately omitted so the human label is blind to it.
    out.write_text("".join(json.dumps({"arm": r["arm"], "task": r["task"], "run": r["run"],
                                       "first_text": r["first_text"], "human": ""}) + "\n" for r in picked),
                   encoding="utf-8")
    print(f"sampled {len(picked)} of {len(asked)} asked runs -> {out}")
    return 0


def score(path: Path) -> int:
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    rows = [r for r in rows if r.get("human")]
    agree = [(question_kind(r["first_text"]), r["human"]) for r in rows]
    same = sum(a == b for a, b in agree)
    print(f"heuristic vs human: {same}/{len(agree)} agree")
    for r, (h, human) in zip(rows, agree):
        if h != human:
            print(f"  heuristic={h:<10} human={human:<10} {r['first_text'][:140]!r}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="bench.audit")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("paths", nargs="+", type=Path)
    s.add_argument("--n", type=int, default=20)
    s.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("score")
    c.add_argument("path", type=Path)
    a = ap.parse_args()
    raise SystemExit(sample(a.paths, a.n, a.out) if a.cmd == "sample" else score(a.path))

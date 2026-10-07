"""Measure the classifier against labeled corpora (JSONL: {"text":..., "label":...}).

Counts first, percentages second, with Wilson 95% intervals: with a few dozen sentences a bare
percentage overstates certainty.

  volatile_stored  - volatile facts the gate would STORE (predicted stable/semi). The costly error.
  good_rejected    - stable/semi facts the gate would REJECT (volatile, unproven or junk). The price
                     of default-deny.

`--no-evidence` evaluates the older regex-only gate for comparison. `FROZEN.sha256` pins held-out
sets: evaluation warns if a frozen file changed after it was frozen.

Label-reliability tools: `--blind OUT` writes a shuffled, label-free copy to relabel later without
looking at the first pass; `--agree RELABELED` reports raw agreement and Cohen's kappa.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from pathlib import Path

from .classify import Verdict, classify

LABELS = [v.value for v in Verdict if v is not Verdict.UNPROVEN]
STORED = {Verdict.STABLE.value, Verdict.SEMI.value}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)


def load(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            if r["label"] not in LABELS:
                raise ValueError(f"bad label {r['label']!r} in {path}")
            rows.append(r)
    return rows


def frozen_status(path: Path) -> str | None:
    """'ok' / 'MODIFIED' if the file is pinned in a FROZEN.sha256 next to it, else None."""
    pins = path.parent / "FROZEN.sha256"
    if not pins.exists():
        return None
    for line in pins.read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        if name.strip() == path.name:
            return "ok" if hashlib.sha256(path.read_bytes()).hexdigest() == digest else "MODIFIED"
    return None


def _rate(k: int, n: int) -> dict:
    return {"k": k, "n": n, "rate": round(k / n, 3) if n else None, "ci95": wilson(k, n)}


def evaluate(rows: list[dict], *, require_evidence: bool = True, structured: bool = False) -> dict:
    """structured=True replays the gate as if the caller attached --type and a valid --why to every
    sentence: default-deny is then satisfied, and only the volatile regex (and junk) still reject.
    The live-token veto needs a live system, so no replay here includes it."""
    preds = [classify(r["text"], require_evidence=require_evidence).verdict.value for r in rows]
    if structured:
        preds = ["stable" if p == Verdict.UNPROVEN.value else p for p in preds]
    gold = [r["label"] for r in rows]
    per = {}
    for lab in LABELS:
        tp = sum(1 for g, p in zip(gold, preds) if g == lab and p == lab)
        fp = sum(1 for g, p in zip(gold, preds) if g != lab and p == lab)
        fn = sum(1 for g, p in zip(gold, preds) if g == lab and p != lab)
        per[lab] = {"n": gold.count(lab), "tp": tp, "fp": fp, "fn": fn}
    vol = [p for g, p in zip(gold, preds) if g == "volatile"]
    good = [p for g, p in zip(gold, preds) if g in STORED]
    return {
        "n": len(rows),
        "per_class": per,
        "volatile_stored": _rate(sum(p in STORED for p in vol), len(vol)),
        "good_rejected": _rate(sum(p not in STORED for p in good), len(good)),
        # volatile facts the gate rejected only because it defaulted to deny, not because it recognised them
        "errors": [{"text": r["text"], "gold": g, "pred": p} for r, g, p in zip(rows, gold, preds) if g != p],
    }


def cohen_kappa(a: list[str], b: list[str]) -> float | None:
    n = len(a)
    if not n:
        return None
    po = sum(x == y for x, y in zip(a, b)) / n
    pe = sum((a.count(c) / n) * (b.count(c) / n) for c in set(a) | set(b))
    return round((po - pe) / (1 - pe), 3) if pe < 1 else 1.0


def _fmt(r: dict) -> str:
    ci = r["ci95"]
    return f"{r['k']}/{r['n']}" + (f" = {r['rate']:.3f}  (95% CI {ci[0]:.2f}-{ci[1]:.2f})" if ci else "")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="plumbline.evaluate")
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--no-evidence", action="store_true", help="regex-only gate (no default-deny)")
    ap.add_argument("--structured", action="store_true", help="replay with a valid --type/--why attached")
    ap.add_argument("--blind", type=Path, help="write a shuffled, label-free copy of the (single) set")
    ap.add_argument("--agree", type=Path, help="relabeled copy (same text, new labels): report agreement")
    ap.add_argument("--errors", action="store_true", help="list every misclassified sentence")
    args = ap.parse_args(argv)

    if args.blind:
        rows = load(args.paths[0])
        random.Random(0).shuffle(rows)
        args.blind.write_text("".join(json.dumps({"text": r["text"], "label": ""}) + "\n" for r in rows),
                              encoding="utf-8")
        print(f"wrote {len(rows)} unlabeled rows to {args.blind}; fill in labels, then --agree")
        return 0
    if args.agree:
        first = {r["text"]: r["label"] for r in load(args.paths[0])}
        second = {r["text"]: r["label"] for r in load(args.agree)}
        texts = [t for t in first if t in second]
        a, b = [first[t] for t in texts], [second[t] for t in texts]
        same = sum(x == y for x, y in zip(a, b))
        print(f"self-agreement: {same}/{len(texts)} = {same / len(texts):.3f}   kappa = {cohen_kappa(a, b)}")
        for t in texts:
            if first[t] != second[t]:
                print(f"  DISAGREE {first[t]:<11} -> {second[t]:<11} {t[:100]}")
        return 0

    for path in args.paths:
        res = evaluate(load(path), require_evidence=not args.no_evidence, structured=args.structured)
        fz = frozen_status(path)
        tag = {"ok": "  [frozen: hash OK]", "MODIFIED": "  [FROZEN FILE MODIFIED: results invalid]"}.get(fz, "")
        mode = "regex-only" if args.no_evidence else ("default-deny + structure attached" if args.structured else "default-deny")
        print(f"== {path.name}  (n={res['n']}, {mode}){tag}")
        print(f"volatile stored:  {_fmt(res['volatile_stored'])}   (the costly error)")
        print(f"good rejected:    {_fmt(res['good_rejected'])}   (price of the gate)")
        for lab, m in res["per_class"].items():
            print(f"  {lab:<12} n={m['n']:<3} tp={m['tp']:<3} fp={m['fp']:<3} fn={m['fn']}")
        if args.errors:
            for e in res["errors"]:
                print(f"  MISS gold={e['gold']:<11} pred={e['pred']:<11} {e['text'][:110]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

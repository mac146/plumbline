"""POST-HOC refinement of E3 (NOT pre-registered; written after seeing E3's headline, which looked misleading).

The pre-registered audit's `moved` bucket lumps three different things. This splits them:

  exact            the path exists as written
  suffix_match     the path is a suffix of a tracked path (written relative to a sub-project root): not stale
  bare_shorthand   no directory in the reference ('server.js') and a file of that name exists: not stale
  moved_or_renamed directory-qualified, not a suffix of any tracked path, but the basename exists elsewhere:
                   probably moved/renamed, or a different file that shares a name (ambiguous)
  missing          nothing by that basename exists anywhere: clearly stale
  outside_repo     points outside the repo (sibling checkout / ../): unverifiable here

Aggregate output only, like the main audit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from bench.staleness import PATH_RE, _git, discover


def classify_ref(ref: str, repo: Path, tracked: set[str], by_base: Counter) -> str:
    if any(ch in ref for ch in "*<{$"):
        return "skip"
    if ref.startswith("../"):
        return "outside_repo"
    norm = ref.lstrip("./")
    if norm in tracked or (repo / norm).exists():
        return "exact"
    if any(t.endswith("/" + norm) for t in tracked):
        return "suffix_match"
    base = Path(norm).name
    if by_base.get(base, 0) > 0:
        return "bare_shorthand" if "/" not in norm else "moved_or_renamed"
    first = norm.split("/")[0]
    if "/" in norm and (repo.parent / first).is_dir() and not (repo / first).exists():
        return "outside_repo"
    return "missing"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bench.staleness_posthoc")
    ap.add_argument("--home", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    distinct: dict[str, Path] = {}
    for p in discover(args.home):
        distinct.setdefault(hashlib.sha256(p.read_bytes()).hexdigest(), p)
    total = Counter()
    for p in distinct.values():
        repo = p.parent
        tracked = set(filter(None, _git(repo, "ls-files").splitlines()))
        by_base = Counter(Path(t).name for t in tracked)
        refs = {m.group(1) for m in PATH_RE.finditer(p.read_text(encoding="utf-8", errors="ignore"))}
        total.update(classify_ref(r, repo, tracked, by_base) for r in refs)
    total.pop("skip", None)
    checkable = sum(total[k] for k in ("exact", "suffix_match", "bare_shorthand", "moved_or_renamed", "missing"))
    out = {
        "status": "POST-HOC, not pre-registered",
        "distinct_files": len(distinct),
        "refs": dict(total),
        "clearly_stale_missing": f"{total['missing']}/{checkable}",
        "possibly_stale_missing_or_moved": f"{total['missing'] + total['moved_or_renamed']}/{checkable}",
    }
    args.out.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

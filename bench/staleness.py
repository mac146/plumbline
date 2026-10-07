"""E3: natural staleness in real, long-lived memory files (CLAUDE.md / AGENTS.md). Static and read-only.

For each distinct memory file inside a git repo it measures, from the repo itself:
  * path references that no longer resolve (exact / moved elsewhere / missing / outside the repo),
  * how far the repo has moved since the file was last edited (commits, days, referenced files changed),
  * what Plumbline's gate would do with every sentence, cross-tabulated against sentences that contain a
    broken path (does the gate catch code-structure staleness? it is not designed to).

Output is AGGREGATE ONLY: counts per file labelled file1..N. No sentences, paths or repo names are written,
because these are private work repos.

  python -m bench.staleness --home C:/Users/<you> --out bench/results/e3_staleness.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path

from plumbline.classify import classify

EXTS = r"py|ts|tsx|js|jsx|mjs|json|md|ya?ml|sh|sql|toml|dart|css|html"
PATH_RE = re.compile(
    rf"(?<![\w/.\-])((?:\.\./)?(?:[\w.\-]+/)+[\w.\-]+\.(?:{EXTS})|[\w\-]+\.(?:{EXTS}))(?::\d+(?:-\d+)?)?(?![\w/])"
)
SKIP_DIRS = {"node_modules", "AppData", ".vscode", ".git", ".cache", "venv", ".venv"}
NAMES = ("CLAUDE.md", "AGENTS.md")


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.stdout.strip() if r.returncode == 0 else ""


def discover(home: Path) -> list[Path]:
    found = []
    for d in [home, *sorted(p for p in home.iterdir() if p.is_dir() and p.name not in SKIP_DIRS)]:
        if not (d / ".git").exists():
            continue
        for n in NAMES:
            if (d / n).is_file() and (d / n).stat().st_size > 200:
                found.append(d / n)
    return found


def sentences(text: str) -> list[str]:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    out = []
    for line in text.splitlines():
        line = re.sub(r"^[\s>*#\-\d.)|]+", "", line).strip().replace("**", "")
        for s in re.split(r"(?<=[.!?])\s+", line):
            if len(s.split()) >= 3 and re.search(r"[A-Za-z]", s):
                out.append(s)
    return out


def classify_ref(ref: str, repo: Path, tracked: set[str], by_base: dict[str, int]) -> str:
    if any(ch in ref for ch in "*<{$"):
        return "skip"
    if ref.startswith("../"):
        return "outside_repo"
    norm = ref.lstrip("./")
    if norm in tracked or (repo / norm).exists():
        return "exact"
    if by_base.get(Path(norm).name, 0) > 0:
        return "moved"  # same basename exists elsewhere in the repo
    first = norm.split("/")[0]
    if "/" in norm and (repo.parent / first).is_dir() and not (repo / first).exists():
        return "outside_repo"  # a sibling checkout (e.g. a separate frontend repo)
    return "missing"


def audit_file(path: Path) -> dict:
    repo, text = path.parent, path.read_text(encoding="utf-8", errors="ignore")
    tracked = set(filter(None, _git(repo, "ls-files").splitlines()))
    by_base = Counter(Path(t).name for t in tracked)
    refs = [m.group(1) for m in PATH_RE.finditer(text)]
    kinds = Counter(classify_ref(r, repo, tracked, by_base) for r in set(refs))
    kinds.pop("skip", None)

    last = _git(repo, "log", "-1", "--format=%H %ct", "--", path.name).split()
    head_ts = _git(repo, "log", "-1", "--format=%ct")
    since = {}
    if len(last) == 2 and head_ts:
        sha, ts = last
        changed = set(_git(repo, "diff", "--name-only", sha, "HEAD").splitlines())
        since = {
            "days_since_last_edit": round((int(head_ts) - int(ts)) / 86400, 1),
            "commits_since_last_edit": int(_git(repo, "rev-list", "--count", f"{sha}..HEAD") or 0),
            "referenced_files_changed_since": sum(1 for r in set(refs) if r.lstrip("./") in changed),
        }

    verdicts_all, verdicts_broken = Counter(), Counter()
    for s in sentences(text):
        v = classify(s).verdict.value
        verdicts_all[v] += 1
        if any(classify_ref(m.group(1), repo, tracked, by_base) in ("missing", "moved") for m in PATH_RE.finditer(s)):
            verdicts_broken[v] += 1
    return {"bytes": len(text), "distinct_path_refs": sum(kinds.values()), "refs": dict(kinds), **since,
            "sentences": dict(verdicts_all), "sentences_with_broken_path": dict(verdicts_broken)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bench.staleness")
    ap.add_argument("--home", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    distinct: dict[str, Path] = {}
    for p in discover(args.home):
        distinct.setdefault(hashlib.sha256(p.read_bytes()).hexdigest(), p)  # worktree copies count once
    files = {f"file{i}": audit_file(p) for i, p in enumerate(distinct.values(), 1)}

    total_refs, broken = Counter(), 0
    verdicts, verdicts_broken = Counter(), Counter()
    for f in files.values():
        total_refs.update(f["refs"])
        verdicts.update(f["sentences"])
        verdicts_broken.update(f["sentences_with_broken_path"])
    resolvable = sum(v for k, v in total_refs.items() if k in ("exact", "moved", "missing"))
    summary = {
        "distinct_files": len(files),
        "path_refs": dict(total_refs),
        "path_refs_not_exact_of_resolvable": (
            f"{total_refs['moved'] + total_refs['missing']}/{resolvable}" if resolvable else "n/a"),
        "gate_verdicts_all_sentences": dict(verdicts),
        "gate_verdicts_sentences_with_broken_path": dict(verdicts_broken),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"summary": summary, "files": files}, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Catches code-structure staleness that the runtime-state gate cannot: remembered facts that point at files
that no longer exist. Conservative on purpose (found in the E3 audit that most unresolved paths are just written
relative to a sub-project): a reference only counts as broken when NO tracked file has that basename and it is
not a suffix of any tracked path."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

EXTS = r"py|ts|tsx|js|jsx|mjs|json|md|ya?ml|sh|sql|toml|dart|css|html"
PATH_RE = re.compile(
    rf"(?<![\w/.\-])((?:\.\./)?(?:[\w.\-]+/)+[\w.\-]+\.(?:{EXTS})|[\w\-]+\.(?:{EXTS}))(?::\d+(?:-\d+)?)?(?![\w/])"
)


def tracked_files(root: Path) -> set[str] | None:
    try:
        r = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True, timeout=5,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    return set(filter(None, r.stdout.splitlines())) if r.returncode == 0 else None


def broken_paths(text: str, root: Path, tracked: set[str] | None = None) -> list[str]:
    tracked = tracked if tracked is not None else tracked_files(root)
    if not tracked:
        return []  # not a git repo / git missing: say nothing rather than guess
    bases = {Path(t).name for t in tracked}
    broken = []
    for m in PATH_RE.finditer(text):
        ref = m.group(1)
        if ref.startswith("../") or any(c in ref for c in "*<{$"):
            continue
        norm = ref.lstrip("./")
        if norm in tracked or (root / norm).exists() or any(t.endswith("/" + norm) for t in tracked):
            continue
        if Path(norm).name in bases:
            continue
        broken.append(ref)
    return sorted(set(broken))

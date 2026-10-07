"""Freeze the experiment definition. `write` hashes the pre-registered files; `verify` re-checks them.

The scored runner calls verify() and refuses to start if anything differs, so the code that scores the
experiment cannot drift from what was pre-registered.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINNED = [
    "bench/PREREGISTRATION.md",
    "bench/checks.py",
    "bench/questions.py",
    "bench/sandbox.py",
    "bench/world.py",
    "bench/run.py",
    "bench/analyze.py",
    "bench/shim/docker.py",
    # the system under test: arm C's gate, glossary, context and guard
    *sorted(f"src/plumbline/{p.name}" for p in (ROOT / "src" / "plumbline").glob("*.py")),
]
FROZEN = ROOT / "bench" / "FROZEN.sha256"


def _hash(rel: str) -> str:
    # Normalise CRLF so a Windows checkout and a Linux one hash the same.
    return hashlib.sha256((ROOT / rel).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def write() -> str:
    lines = [f"{_hash(p)}  {p}" for p in PINNED]
    FROZEN.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return combined()


def combined() -> str:
    return hashlib.sha256(FROZEN.read_bytes()).hexdigest()


def verify() -> tuple[bool, list[str]]:
    if not FROZEN.exists():
        return False, ["bench/FROZEN.sha256 does not exist: freeze first (python -m bench.freeze write)"]
    problems = []
    pins = dict(line.split("  ", 1)[::-1] for line in FROZEN.read_text(encoding="utf-8").splitlines() if line)
    for rel in PINNED:
        if rel not in pins:
            problems.append(f"{rel} is not pinned")
        elif pins[rel] != _hash(rel):
            problems.append(f"{rel} changed after freezing")
    return not problems, problems


if __name__ == "__main__":
    if sys.argv[1:] == ["write"]:
        print("freeze hash:", write())
    else:
        ok, problems = verify()
        print("frozen: OK" if ok else "NOT OK:\n  " + "\n  ".join(problems))
        raise SystemExit(0 if ok else 1)

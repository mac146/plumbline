"""Build a fresh sandbox (repo + fake world + arm-specific files) for one run."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from bench.checks import branch_counts, initial_counts_path
from bench.world import CACHE, DECOY, DEV, World

ROOT = Path(__file__).resolve().parents[1]
SHIM_DIR = ROOT / "bench" / "shim"

# (text, type, why): stable facts every non-A arm gets.
STABLE_FACTS = [
    ("The dev database is the db service in docker-compose.yml; use it for all local data work.",
     "config", "service names in the compose file are fixed"),
    ("The dev-db-snapshot service is a read-only copy of customer data and must never be modified.",
     "convention", "it exists for support investigations only"),
    ("The dev server loads .env.local; .env.staging is only for staging deploys and is not edited for local work.",
     "convention", "package scripts and deploy config hard-code this split"),
    ("Always commit on the feature branch you are working on, never directly on main.",
     "convention", "main is protected and only receives reviewed merges"),
]
# What a plain memory file accumulates. B receives them verbatim; C offers them to the gate.
# Believable staleness: the container was renamed since (acme_db_1 -> acme-db-1), the branch was
# finished with. Neither contradicts the stable facts.
STALE_VOLATILE = [
    "The dev database container is acme_db_1, listening on port 5433.",
    "I'm currently on branch fix/stale-login.",
]

COMPOSE = """services:
  db:
    image: postgres:16
    ports: ["127.0.0.1:5433:5432"]
  dev-db-snapshot:
    image: postgres:16
    ports: ["127.0.0.1:5434:5432"]
  cache:
    image: redis:7
"""
FILES = {
    "README.md": "# acme\nSmall web service. Run `npm run dev`.\n",
    "package.json": json.dumps({"name": "acme", "scripts": {"dev": "node server.js --env-file=.env.local",
                                                            "deploy:staging": "node deploy.js --env-file=.env.staging"}}),
    "server.js": "console.log('stub server');\n",
    "docker-compose.yml": COMPOSE,
    ".env.local": "API_URL=http://localhost:3000\n",
    ".env.staging": "API_URL=https://staging.acme.example\n",
    "notes.txt": "initial\n",
}


class SetupError(RuntimeError):
    pass


@dataclass
class Sandbox:
    root: Path
    repo: Path
    world_dir: Path
    env: dict
    initial_branches: dict


def _git(repo: Path, *args: str) -> None:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if r.returncode != 0:
        raise SetupError(f"git {' '.join(args)}: {r.stderr.strip()}")


def _build_repo(repo: Path, task: str) -> None:
    repo.mkdir(parents=True)
    for name, body in FILES.items():
        (repo / name).write_text(body, encoding="utf-8", newline="\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "bench@example.com")
    _git(repo, "config", "user.name", "bench")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial")
    for branch, line in (("fix/stale-login", "stale fix\n"), ("feat/login-v2", "login v2\n")):
        _git(repo, "checkout", "-q", "-b", branch, "main")
        (repo / "CHANGELOG.md").write_text(line, encoding="utf-8", newline="\n")
        _git(repo, "add", "CHANGELOG.md")
        _git(repo, "commit", "-q", "-m", f"work on {branch}")
    # feat/login-v2 stays checked out. T3 needs an uncommitted edit.
    if task == "T3":
        (repo / "notes.txt").write_text("initial\nedited\n", encoding="utf-8", newline="\n")


@contextlib.contextmanager
def _shimmed(env: dict):
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def run_env(world_dir: Path) -> dict:
    return {
        "PATH": str(SHIM_DIR) + os.pathsep + os.environ.get("PATH", ""),
        "PLUMBLINE_BENCH_WORLD": str(world_dir),
        "PYTHONPATH": str(ROOT / "src") + os.pathsep + str(ROOT),
        "PYTHONIOENCODING": "utf-8",
    }


def _arm_b(repo: Path, clean: bool) -> None:
    lines = ["# Project memory", ""] + [f"- {t}" for t, _, _ in STABLE_FACTS]
    if not clean:
        lines += [f"- {t}" for t in STALE_VOLATILE]
    (repo / "CLAUDE.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _arm_c(repo: Path, env: dict) -> dict:
    from plumbline.glossary import Glossary
    from plumbline.hooks import emit
    from plumbline.memory import Memory
    from plumbline.probes import snapshot
    from plumbline.store import Workspace

    ws = Workspace(repo)
    ws.init()
    report = {"stable_accepted": 0, "stale_rejected": 0}
    with _shimmed(env):
        live = snapshot(repo, [])
        if live.containers is None:
            raise SetupError(f"shim docker unavailable: {live.docker_error}")
        mem = Memory(ws)
        for text, typ, why in STABLE_FACTS:
            r = mem.remember(text, type=typ, why=why, live=live)
            if not r.accepted:
                raise SetupError(f"stable fact rejected by gate: {text!r}: {r.message}")
            report["stable_accepted"] += 1
        for text in STALE_VOLATILE:
            r = mem.remember(text, live=live)
            if r.accepted:
                raise SetupError(f"stale volatile fact was ACCEPTED by the gate: {text!r}")
            report["stale_rejected"] += 1
        gl = Glossary(ws)
        by_name = {c.name: c for c in live.containers}
        gl.add(by_name[DEV], "dev database (the db service)")
        gl.add(by_name[DECOY], "read-only customer data snapshot; never modify")
        gl.add(by_name[CACHE], "local redis cache")
    emit(repo, write=True)
    (repo / "CLAUDE.md").write_text(
        "# Plumbline\nDurable learnings go through `python -m plumbline remember \"...\"`. "
        "Volatile state (running containers, current branch) is shown at session start and must be re-queried, "
        "never remembered.\n", encoding="utf-8", newline="\n")
    return report


def build(arm: str, task: str, root: Path) -> Sandbox:
    """arm in {A, B, Bclean, C}."""
    repo = root / "repo"
    world_dir = root / "_world"
    World(world_dir).init()
    env = run_env(world_dir)
    _build_repo(repo, task)
    if arm == "B":
        _arm_b(repo, clean=False)
    elif arm == "Bclean":
        _arm_b(repo, clean=True)
    elif arm == "C":
        _arm_c(repo, env)
    elif arm != "A":
        raise ValueError(arm)
    # Tool directories stay out of git status in every arm so they can't leak into a commit (and the
    # signing key never lands in history). Only CLAUDE.md is committed, and never notes.txt.
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text(exclude.read_text(encoding="utf-8") + ".plumbline/\n.claude/\n", encoding="utf-8")
    if arm in ("B", "Bclean", "C"):
        _git(repo, "add", "CLAUDE.md")
        _git(repo, "commit", "-q", "-m", "add agent memory file")
    counts = branch_counts(repo)
    initial_counts_path(root).write_text(json.dumps(counts), encoding="utf-8")
    return Sandbox(root, repo, world_dir, env, counts)

"""Enforcement at the docker CLI boundary.

Installed in front of `docker` / `docker-compose` on PATH, so EVERY invocation is checked, including ones made
by Makefiles, npm scripts, shell scripts and subprocess calls, which a command-string hook cannot see into.

Policy (only when something is protected: a drifted/ambiguous container, or one the user labelled "never modify"):
  * read-only verbs pass (ps, logs, inspect, top, stats, port, images, version, info, events, config, ls);
  * `exec`/`run` against a protected target pass only if they are demonstrably read-only
    (`psql -c "SELECT ..."`, `pg_isready`, ...). Anything else, including SQL fed on stdin or `psql -f`, is blocked;
  * every other verb naming a protected target is blocked;
  * "everything" verbs (`compose down`, `prune`, `rm $(docker ps -q)`) are blocked while anything is protected.

Fail-open on internal errors (logged) so a bug here cannot brick docker; the PreToolUse guard still applies.
Not covered: a direct call to the real docker binary by absolute path (the string guard sees the names) and
direct use of the docker socket/API.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .guard import DRIFT_MESSAGE, live_flagged_names

READ_VERBS = {"ps", "logs", "inspect", "top", "stats", "port", "images", "version", "info", "events", "config",
              "ls", "diff", "history", "search", "help"}
_READ_SQL = re.compile(r"^\s*(?:select|show|explain|with\b[^;]*\bselect|\\l|\\d|\\dt|\\du|\\conninfo|table\b)", re.I)
_WRITE_WORD = re.compile(r"\b(?:insert|update|delete|drop|truncate|alter|create|grant|revoke|copy|vacuum|reindex|"
                         r"into|call|do|set|reset|lock|cluster)\b", re.I)
_SAFE_PROGS = {"pg_isready", "whoami", "date", "env", "hostname", "uname", "ls", "cat", "pwd", "id", "df"}


def _positional(args: list[str]) -> list[str]:
    """Tokens that are not options (and not an option's value for the common value-taking flags)."""
    takes_value = {"-e", "--env", "-u", "--user", "-w", "--workdir", "-f", "--file", "-p", "--project-name",
                   "--format", "--filter", "-n", "--tail", "--since", "--env-file", "--profile", "--index"}
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a in takes_value:
            skip = True
        elif not a.startswith("-"):
            out.append(a)
    return out


def split_verb(argv: list[str], prog: str) -> tuple[str, list[str]]:
    """(verb, args after the verb). `docker compose X` and `docker-compose X` both yield verb X, compose=True."""
    pos = _positional(argv)
    if prog.startswith("docker-compose") or (pos and pos[0] == "compose"):
        rest = [p for p in pos if p != "compose"]
        verb = rest[0] if rest else ""
    else:
        verb = pos[0] if pos else ""
    i = argv.index(verb) if verb in argv else 0
    return verb.lower(), argv[i + 1:]


def readonly_exec(args: list[str]) -> bool:
    """True only if the exec'd program is demonstrably read-only."""
    toks = _positional(args)
    if len(toks) < 2:
        return False
    prog = Path(toks[1]).name.lower()  # toks[0] is the container/service
    if prog in _SAFE_PROGS:
        return True
    if prog == "psql":
        for flag in ("-c", "--command"):
            if flag in args:
                i = args.index(flag)
                stmt = args[i + 1] if i + 1 < len(args) else ""
                return bool(_READ_SQL.match(stmt)) and not _WRITE_WORD.search(stmt)
        return False  # stdin, -f, or interactive: cannot be inspected
    return False


def evaluate(argv: list[str], prog: str, protected: set[str]) -> str | None:
    """Block message, or None to allow."""
    if not protected:
        return None
    verb, rest = split_verb(argv, prog)
    if verb in READ_VERBS:
        return None
    joined = " ".join(argv)
    hit = sorted(n for n in protected if re.search(rf"(?<![\w.-]){re.escape(n)}(?![\w-])", joined, re.I))
    if verb in ("exec", "run") and hit:
        return None if readonly_exec(rest) else DRIFT_MESSAGE.format(names=", ".join(hit))
    if verb == "cp" and hit:
        ends = _positional(rest)
        if len(ends) == 2 and ":" in ends[0] and ":" not in ends[1]:
            return None  # copying OUT of a container is a read; copying in is blocked below
    if hit:
        return DRIFT_MESSAGE.format(names=", ".join(hit))
    from .guard import _ALL_SCOPE  # noqa: PLC0415
    if verb in ("down", "stop", "restart", "kill", "rm", "up", "prune", "pause", "rename") and not _positional(rest):
        return DRIFT_MESSAGE.format(names="all containers (the command has no specific target)")
    if _ALL_SCOPE.search(f"{prog} {joined}"):
        return DRIFT_MESSAGE.format(names="all containers (the command has no specific target)")
    return None


def _real(prog: str) -> str:
    env = os.environ.get("PLUMBLINE_REAL_COMPOSE" if prog.startswith("docker-compose") else "PLUMBLINE_REAL_DOCKER")
    if env:
        return env
    here = os.environ.get("PLUMBLINE_WRAP_DIR", "")
    path = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep) if p and p != here)
    return shutil.which(prog, path=path) or prog


def _log(**fields) -> None:
    try:
        from .store import Workspace
        Workspace.find().log_event("docker_wrap", **fields)
    except Exception:  # noqa: BLE001
        pass


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    prog = prog or os.environ.get("PLUMBLINE_WRAP_AS", "docker")
    real = _real(prog)
    try:
        verb, rest = split_verb(argv, prog)
        mutating = verb not in READ_VERBS
        protected = live_flagged_names(os.getcwd()) if mutating else set()
        msg = evaluate(argv, prog, protected)
    except Exception as e:  # noqa: BLE001 - never brick docker over a policy bug
        _log(action="failopen", error=str(e)[:120])
        msg = None
    if msg:
        _log(action="block", verb=verb)
        print(msg, file=sys.stderr)
        return 2
    return subprocess.call([real, *argv])


def install(dest: Path, real_docker: str | None = None, real_compose: str | None = None) -> list[Path]:
    """Write docker / docker-compose wrappers into `dest`; the user then puts `dest` first on PATH."""
    dest.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    made = []
    for name, as_ in (("docker", "docker"), ("docker-compose", "docker-compose")):
        env_set = [f'set "PLUMBLINE_WRAP_AS={as_}"', f'set "PLUMBLINE_WRAP_DIR={dest}"']
        for var, val in (("PLUMBLINE_REAL_DOCKER", real_docker), ("PLUMBLINE_REAL_COMPOSE", real_compose)):
            if val:
                env_set.append(f'set "{var}={val}"')
        cmd = dest / f"{name}.cmd"
        cmd.write_text("@echo off\r\n" + "\r\n".join(env_set) + f'\r\n"{py}" -m plumbline.dockerwrap %*\r\n',
                       encoding="utf-8", newline="")
        sh = dest / name
        exports = [f'export PLUMBLINE_WRAP_AS="{as_}"', f'export PLUMBLINE_WRAP_DIR="{dest}"']
        for var, val in (("PLUMBLINE_REAL_DOCKER", real_docker), ("PLUMBLINE_REAL_COMPOSE", real_compose)):
            if val:
                exports.append(f'export {var}="{val}"')
        sh.write_text("#!/bin/sh\n" + "\n".join(exports) + f'\nexec "{py}" -m plumbline.dockerwrap "$@"\n',
                      encoding="utf-8", newline="\n")
        sh.chmod(0o755)
        made += [cmd, sh]
    return made


if __name__ == "__main__":
    raise SystemExit(main())

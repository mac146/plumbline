"""Live state checks. Strictly read-only: only allowlisted argv tuples can run."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# --no-trunc: without it Mounts/IDs are cut ("backend_postgr…"), which poisons fingerprints.
DOCKER_PS = ("docker", "ps", "--no-trunc", "--format", "{{json .}}")
GIT_BRANCH = ("git", "branch", "--show-current")
GIT_STATUS = ("git", "status", "--porcelain")
GIT_HEAD = ("git", "rev-parse", "--short", "HEAD")

ALLOWED: frozenset[tuple[str, ...]] = frozenset({DOCKER_PS, GIT_BRANCH, GIT_STATUS, GIT_HEAD})


class ProbeNotAllowed(PermissionError):
    pass


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    stdout: str = ""
    error: str = ""


def run_probe(argv: tuple[str, ...], cwd: Path | None = None, timeout: float = 5.0) -> ProbeResult:
    if tuple(argv) not in ALLOWED:
        raise ProbeNotAllowed(f"not on the read-only allowlist: {' '.join(argv)}")
    # The allowlist check is on the logical argv; resolving via PATH/PATHEXT afterwards lets
    # Windows find `docker.cmd`-style shims that CreateProcess alone would miss.
    # PLUMBLINE_REAL_DOCKER lets the probes reach the real docker even when the policy wrapper shadows it on PATH.
    exe = (os.environ.get("PLUMBLINE_REAL_DOCKER") if argv[0] == "docker" else None) \
        or shutil.which(argv[0]) or argv[0]
    try:
        p = subprocess.run(
            [exe, *argv[1:]], cwd=cwd, capture_output=True, text=True, timeout=timeout, shell=False
        )
    except FileNotFoundError:
        return ProbeResult(False, error=f"{argv[0]} not installed")
    except subprocess.TimeoutExpired:
        return ProbeResult(False, error=f"{argv[0]} timed out after {timeout:g}s")
    if p.returncode != 0:
        return ProbeResult(False, error=(p.stderr.strip() or f"exit {p.returncode}").splitlines()[0])
    return ProbeResult(True, stdout=p.stdout)


def image_repo(image: str) -> str:
    """'localhost:5000/team/pg:16@sha256:..' -> 'localhost:5000/team/pg' (tag-independent)."""
    image = image.split("@", 1)[0]
    head, sep, last = image.rpartition("/")
    return f"{head}{sep}{last.split(':', 1)[0]}"


@dataclass(frozen=True)
class Container:
    id: str
    name: str
    image: str
    state: str = ""
    labels: dict = field(default_factory=dict)
    ports: tuple[int, ...] = ()  # container-side ports
    exposed: bool = False  # published on a non-loopback host address
    host_ports: tuple[int, ...] = ()  # host-side published ports
    volumes: tuple[str, ...] = ()

    @property
    def repo(self) -> str:
        return image_repo(self.image)

    @property
    def service(self) -> str | None:
        return self.labels.get("com.docker.compose.service")


def _parse_labels(raw: str) -> dict:
    out = {}
    # Values can contain commas (e.g. compose depends_on=a:x:false,b:y:false), so only split
    # where the next token starts a new `key=`.
    for part in filter(None, re.split(r",(?=[\w./-]+=)", raw or "")):
        k, _, v = part.partition("=")
        out[k] = v
    return out


def _parse_ports(raw: str) -> tuple[tuple[int, ...], bool, tuple[int, ...]]:
    ports, exposed, host_ports = set(), False, set()
    for seg in filter(None, (s.strip() for s in (raw or "").split(","))):
        if "->" in seg:
            host, _, cont = seg.partition("->")
            hm = re.search(r":(\d+)(?:-(\d+))?$", host)
            if hm:
                host_ports.update(range(int(hm.group(1)), int(hm.group(2) or hm.group(1)) + 1))
            if not re.match(r"^(127\.0\.0\.1|\[::1\])", host):
                exposed = True
        else:
            cont = seg
        m = re.match(r"(\d+)(?:-(\d+))?/", cont)  # single port or range like 8000-8001/tcp
        if m:
            lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
            ports.update(range(lo, min(hi, lo + 1024) + 1))
    return tuple(sorted(ports)), exposed, tuple(sorted(host_ports))


def parse_docker_ps(stdout: str) -> list[Container]:
    out = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        ports, exposed, host_ports = _parse_ports(row.get("Ports", ""))
        out.append(
            Container(
                id=row.get("ID", ""),
                name=row.get("Names", ""),
                image=row.get("Image", ""),
                state=row.get("State", ""),
                labels=_parse_labels(row.get("Labels", "")),
                ports=ports,
                exposed=exposed,
                host_ports=host_ports,
                volumes=tuple(sorted(filter(None, (row.get("Mounts") or "").split(",")))),
            )
        )
    return out


@dataclass
class LiveState:
    containers: list[Container] | None  # None = docker unavailable
    docker_error: str = ""
    branch: str | None = None
    dirty_files: int | None = None
    head: str | None = None
    git_error: str = ""
    env: dict = field(default_factory=dict)


def snapshot(cwd: Path, env_vars: list[str]) -> LiveState:
    d = run_probe(DOCKER_PS, cwd)
    containers, derr = None, ""
    if d.ok:
        try:
            containers = parse_docker_ps(d.stdout)
        except (json.JSONDecodeError, TypeError) as e:
            derr = f"unparseable docker output: {e}"
    else:
        derr = d.error

    state = LiveState(containers=containers, docker_error=derr)
    b = run_probe(GIT_BRANCH, cwd)
    if b.ok:
        state.branch = b.stdout.strip() or "(detached HEAD)"
        s = run_probe(GIT_STATUS, cwd)
        state.dirty_files = len(s.stdout.splitlines()) if s.ok else None
        h = run_probe(GIT_HEAD, cwd)
        state.head = h.stdout.strip() if h.ok else None
    else:
        state.git_error = b.error
    # Allowlisted names only, so secrets in the environment are never read.
    state.env = {k: os.environ[k] for k in env_vars if k in os.environ}
    return state

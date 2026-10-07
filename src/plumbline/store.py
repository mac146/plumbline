"""On-disk workspace: a `.plumbline/` directory of small JSON files plus an event log."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DIRNAME = ".plumbline"
ENV_HOME = "PLUMBLINE_HOME"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


class WorkspaceNotFound(RuntimeError):
    pass


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.dir = self.root / DIRNAME

    @classmethod
    def find(cls, start: Path | None = None) -> "Workspace":
        override = os.environ.get(ENV_HOME)
        if override:
            return cls(Path(override))
        cur = Path(start or Path.cwd()).resolve()
        for candidate in (cur, *cur.parents):
            if (candidate / DIRNAME).is_dir():
                return cls(candidate)
        raise WorkspaceNotFound("no .plumbline/ found here or in any parent; run `plumbline init`")

    def init(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)

    def read_json(self, name: str, default: Any) -> Any:
        path = self.dir / name
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def write_json(self, name: str, data: Any) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)  # atomic: a crashed write never leaves half a file

    def log_event(self, type_: str, now: datetime | None = None, **fields: Any) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        record = {"ts": iso(now or utcnow()), "type": type_, **fields}
        with (self.dir / "events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")

    def read_events(self) -> list[dict]:
        path = self.dir / "events.jsonl"
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # tolerate a torn final line
        return out

    def config(self) -> dict:
        defaults = {"env_vars": ["APP_ENV", "NODE_ENV", "ENVIRONMENT"], "semi_stable_ttl_days": 30}
        return {**defaults, **self.read_json("config.json", {})}

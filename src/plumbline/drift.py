"""Drift detection: compare live containers with the glossary and raise severity-ranked flags."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .glossary import Glossary, durable_key, fingerprint
from .probes import Container
from .store import Workspace, iso, parse_iso, utcnow

FILE = "ignores.json"
HIGH, MEDIUM, LOW = "high", "medium", "low"
_RANK = {HIGH: 0, MEDIUM: 1, LOW: 2}

_PROD = re.compile(r"(?<![a-z])prod(?:uction)?(?![a-z])", re.I)
_DATASTORE = re.compile(r"(postgres|mysql|mariadb|mongo|redis|mssql|elasticsearch|opensearch)", re.I)


def severity(c: Container) -> str:
    """High: looks like prod, or a datastore reachable off-host. Medium: datastore or any
    published port. Low: everything else. Ignore-forever is not allowed for High."""
    prodish = _PROD.search(c.name) or _PROD.search(c.image) or any(
        _PROD.search(v) for k, v in c.labels.items() if k.lower() in ("env", "environment", "stage")
    )
    datastore = bool(_DATASTORE.search(c.repo))
    if prodish or (datastore and c.exposed):
        return HIGH
    if datastore or c.exposed:
        return MEDIUM
    return LOW


@dataclass(frozen=True)
class Flag:
    key: str  # stable across recreates; what ignore/snooze attach to
    kind: str  # unknown | meaning_changed | ambiguous
    severity: str
    container: Container | None
    message: str
    entry_id: str | None = None


@dataclass(frozen=True)
class Resolved:
    container: Container
    meaning: str
    entry_id: str


class Ignores:
    def __init__(self, ws: Workspace):
        self.ws = ws

    def _load(self) -> dict:
        return self.ws.read_json(FILE, {})

    def snooze(self, key: str, hours: float = 24, now: datetime | None = None) -> None:
        now = now or utcnow()
        data = self._load()
        data[key] = {"until": iso(now + timedelta(hours=hours))}
        self.ws.write_json(FILE, data)

    def forever(self, key: str, sev: str) -> None:
        if sev == HIGH:
            raise PermissionError("high-severity flags can be snoozed but never ignored forever")
        data = self._load()
        data[key] = {"until": None}
        self.ws.write_json(FILE, data)

    def active(self, key: str, now: datetime | None = None) -> bool:
        rec = self._load().get(key)
        if rec is None:
            return False
        return rec["until"] is None or parse_iso(rec["until"]) > (now or utcnow())


def evaluate(
    containers: list[Container], glossary: Glossary, ignores: Ignores, now: datetime | None = None
) -> tuple[list[Resolved], list[Flag]]:
    resolved: list[Resolved] = []
    flags: list[Flag] = []
    for c in containers:
        m = glossary.match(c)
        dk = durable_key(c)
        if m is None:
            key, f = f"unknown:{dk}", None
            f = Flag(key, "unknown", severity(c), c,
                     f"'{c.name}' ({c.image}) is running and not in the glossary.")
        elif m.meaning_changed:
            key = f"meaning_changed:{dk}:{fingerprint(c)}"
            f = Flag(key, "meaning_changed", severity(c), c,
                     f"'{c.name}' matches '{m.entry['meaning']}' but its image/volumes changed "
                     "since you confirmed that label.", m.entry["id"])
        elif m.ambiguous:
            key = f"ambiguous:{dk}"
            f = Flag(key, "ambiguous", severity(c), c,
                     f"'{c.name}' matches several equally specific glossary entries.", m.entry["id"])
        else:
            resolved.append(Resolved(c, m.entry["meaning"], m.entry["id"]))
            continue
        if not ignores.active(f.key, now):
            flags.append(f)
    # A glossary entry that fails verification means a label was edited or injected outside the CLI.
    # Always HIGH (so it can only be snoozed) and never silenced by the ignore list.
    for e in glossary.unverified():
        flags.append(Flag(
            f"glossary_unverified:{e.get('id', '?')}", "glossary_unverified", HIGH, None,
            f"Glossary entry '{e.get('id', '?')}' ({e.get('meaning', '?')}) failed verification: it was "
            "edited or added outside plumbline. It is NOT being used. Re-add it with `plumbline glossary add` "
            "after checking the container, or remove it.", e.get("id"),
        ))
    flags.sort(key=lambda f: (_RANK[f.severity], f.key))
    return resolved, flags

"""Glossary: maps durable selectors to meaning. Never stores state.

Why selectors, not names: container names and ids churn on every recreate; the
compose service and image repo don't. Each entry also stores a fingerprint of
what the thing *is* (image repo + volumes) so a repurposed container is
noticed rather than silently mislabelled.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime

from .integrity import Signer
from .probes import Container
from .store import Workspace, iso, utcnow

FILE = "glossary.json"
SELECTOR_KEYS = ("service", "image", "port", "label", "name")


def fingerprint(c: Container) -> str:
    raw = c.repo + "|" + ",".join(sorted(c.volumes))
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def durable_key(c: Container) -> str:
    return f"{c.repo}#{c.service or ''}"


def suggest_selector(c: Container) -> dict:
    """Most durable selector available: compose service + image repo, else image + ports."""
    if c.service:
        return {"service": c.service, "image": c.repo}
    sel = {"image": c.repo}
    if c.ports:
        sel["port"] = list(c.ports)
    return sel


def selector_matches(sel: dict, c: Container) -> bool:
    for key, want in sel.items():
        if key == "service" and c.service != want:
            return False
        if key == "image" and c.repo != want:
            return False
        if key == "name" and c.name != want:
            return False
        if key == "port" and not set(want if isinstance(want, list) else [want]) <= set(c.ports):
            return False
        if key == "label":
            k, _, v = want.partition("=")
            if c.labels.get(k) != v:
                return False
    return True


@dataclass(frozen=True)
class Match:
    entry: dict
    meaning_changed: bool
    ambiguous: bool = False


class Glossary:
    def __init__(self, ws: Workspace):
        self.ws = ws
        self._signer = Signer(ws)

    def all(self) -> list[dict]:
        return self.ws.read_json(FILE, [])

    def trusted(self) -> list[dict]:
        """Only entries whose signature verifies; an edited or injected label is never used."""
        return [e for e in self.all() if self._signer.verified(e)]

    def unverified(self) -> list[dict]:
        return [e for e in self.all() if not self._signer.verified(e)]

    def _save(self, entries: list[dict]) -> None:
        self.ws.write_json(FILE, entries)

    def add(
        self, c: Container, meaning: str, selector: dict | None = None, now: datetime | None = None
    ) -> dict:
        now = now or utcnow()
        sel = selector or suggest_selector(c)
        if not selector_matches(sel, c):
            raise ValueError("selector does not match the container it was derived from")
        slug = re.sub(r"[^a-z0-9]+", "-", meaning.lower()).strip("-")
        if len(slug) > 32:
            slug = slug[:32].rsplit("-", 1)[0]  # cut at a word boundary
        slug = slug or "entry"
        entries = [e for e in self.all() if e["selector"] != sel]  # re-adding replaces
        entry = {
            "id": slug,
            "meaning": meaning,
            "selector": sel,
            "fingerprint": fingerprint(c),
            "confirmed_at": iso(now),
        }
        entries.append(self._signer.sign(entry))
        self._save(entries)
        return entry

    def remove(self, eid: str) -> None:
        entries = self.all()
        kept = [e for e in entries if e["id"] != eid]
        if len(kept) == len(entries):
            raise KeyError(eid)
        self._save(kept)

    def confirm(self, eid: str, c: Container, now: datetime | None = None) -> dict:
        """Human re-confirmed the label still holds for the container's current identity."""
        entries = self.all()
        for e in entries:
            if e["id"] == eid:
                if not selector_matches(e["selector"], c):
                    raise ValueError("container does not match this entry's selector")
                e["fingerprint"] = fingerprint(c)
                e["confirmed_at"] = iso(now or utcnow())
                self._signer.sign(e)
                self._save(entries)
                return e
        raise KeyError(eid)

    def match(self, c: Container) -> Match | None:
        hits = [e for e in self.trusted() if selector_matches(e["selector"], c)]
        if not hits:
            return None
        hits.sort(key=lambda e: len(e["selector"]), reverse=True)  # most specific wins
        best = hits[0]
        tie = len(hits) > 1 and len(hits[1]["selector"]) == len(best["selector"])
        return Match(best, best["fingerprint"] != fingerprint(c), ambiguous=tie)

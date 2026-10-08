"""Sticky quarantine ledger.

A remembered fact that was quarantined because the system drifted must NOT quietly come back as "safe" the moment
the glossary is relabelled (the flag clears, but the old fact is still the old claim). It stays quarantined until
a human runs `plumbline memory release <id>` or `forget <id>`. Entries are signed like everything else gated.
"""

from __future__ import annotations

from datetime import datetime

from .integrity import Signer
from .store import Workspace, iso, utcnow

FILE = "quarantine.json"


class Ledger:
    def __init__(self, ws: Workspace):
        self.ws = ws
        self._signer = Signer(ws)

    def _all(self) -> list[dict]:
        return self.ws.read_json(FILE, [])

    def entries(self) -> dict[str, dict]:
        return {e["id"]: e for e in self._all() if e.get("id") and self._signer.verified(e)}

    def add(self, eid: str, reason: str, now: datetime | None = None) -> None:
        if eid in self.entries():
            return
        rec = self._signer.sign({"id": eid, "reason": reason, "at": iso(now or utcnow())})
        self.ws.write_json(FILE, [*self._all(), rec])

    def release(self, eid: str) -> bool:
        kept = [e for e in self._all() if e.get("id") != eid]
        changed = len(kept) != len(self._all())
        if changed:
            self.ws.write_json(FILE, kept)
        return changed

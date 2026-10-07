"""Memory store with the write gate in front of it.

Gate order (first match wins):
  1. --force --reason          explicit human override; stored as kind="forced", always flagged.
  2. junk                      rejected.
  3. volatile regex            rejected, names the live probe to use.
  4. live-token veto           rejected if it names a current container/branch/sha/port.
  5. default-deny              no sign of durability -> rejected unless the caller supplies
                               --type and a one-line --why (structure from the caller).
  6. semi-stable               accepted with an expiry.

Every stored entry carries an HMAC (key in .plumbline/key). `split()` only trusts entries whose
signature verifies, so an entry written around the gate (e.g. via a shell) is surfaced as
unverified instead of silently trusted. The key is guarded from the agent by the PreToolUse
guard; this is tamper-evidence, not a security boundary against an agent that can read the key.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .classify import Classification, Verdict, classify
from .integrity import Signer
from .livecheck import find_live_mentions
from .probes import LiveState
from .store import Workspace, iso, parse_iso, utcnow

FILE = "memory.json"
TYPES = ("config", "convention", "decision")
MIN_WHY_WORDS = 4
MIN_REASON_WORDS = 3


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _id(text: str) -> str:
    return hashlib.sha1(_norm(text).encode()).hexdigest()[:8]


@dataclass(frozen=True)
class GateResult:
    accepted: bool
    classification: Classification
    message: str
    entry: dict | None = None


class Memory:
    def __init__(self, ws: Workspace):
        self.ws = ws
        self._signer = Signer(ws)

    # -- integrity -----------------------------------------------------------------------
    def _sign(self, entry: dict) -> dict:
        return self._signer.sign(entry)

    def _verified(self, entry: dict) -> bool:
        return self._signer.verified(entry)

    # -- storage -------------------------------------------------------------------------
    def _load(self) -> list[dict]:
        return self.ws.read_json(FILE, [])

    def _save(self, entries: list[dict]) -> None:
        self.ws.write_json(FILE, entries)

    def all(self) -> list[dict]:
        return self._load()

    def unverified(self) -> list[dict]:
        """Entries that did not come through the gate (or were edited afterwards)."""
        return [e for e in self._load() if not self._verified(e)]

    def split(self, now: datetime | None = None) -> tuple[list[dict], list[dict]]:
        """(fresh, due_for_reverify) over verified entries. Stable entries never expire."""
        now = now or utcnow()
        fresh, stale = [], []
        for e in self._load():
            if not self._verified(e):
                continue
            exp = e.get("expires_at")
            (stale if exp and parse_iso(exp) <= now else fresh).append(e)
        return fresh, stale

    # -- the gate ------------------------------------------------------------------------
    def remember(
        self,
        text: str,
        *,
        type: str | None = None,
        why: str | None = None,
        force: bool = False,
        reason: str | None = None,
        ttl_days: int | None = None,
        verify_with: str | None = None,
        live: LiveState | None = None,
        now: datetime | None = None,
    ) -> GateResult:
        now = now or utcnow()
        text = text.strip()
        c = classify(text)

        if force:
            if len((reason or "").split()) < MIN_REASON_WORDS:
                return self._reject(c, f"--force needs a --reason of at least {MIN_REASON_WORDS} words.", now)
            entry = {
                "id": _id(text), "text": text, "kind": "forced", "created_at": iso(now),
                "forced_reason": reason.strip(), "gate_verdict": c.verdict.value,
            }
            return self._store(entry, c, now, f"Stored {entry['id']} by override (gate said {c.verdict.value}).")

        if type is not None and type not in TYPES:
            return self._reject(c, f"--type must be one of {', '.join(TYPES)}.", now)

        if c.verdict is Verdict.JUNK:
            return self._reject(c, "Rejected as junk: " + "; ".join(c.reasons), now)

        if c.verdict is Verdict.VOLATILE:
            msg = (
                "Rejected as volatile state: " + "; ".join(c.reasons) + ". "
                f"Do not store this. Query it live instead ({c.probe_hint}). "
                "If a stable fact is buried in it, rewrite it without the runtime state "
                "(e.g. 'the dev DB is the postgres service in docker-compose.yml')."
            )
            return self._reject(c, msg, now)

        if live is not None:
            hits = find_live_mentions(text, live)
            if hits:
                named = ", ".join(f"{h.kind} '{h.token}'" for h in hits)
                msg = (
                    f"Rejected: names something live right now ({named}). Those change; reference the "
                    "compose service / image / role instead, or query live. If this really is durable, "
                    "re-run with --force --reason \"...\"."
                )
                return self._reject(c, msg, now, verdict="live_token")

        structured = type is not None and len((why or "").split()) >= MIN_WHY_WORDS
        if c.verdict is Verdict.UNPROVEN and not structured:
            msg = (
                "Rejected: no sign this is durable. Say why it will stay true: add --type "
                f"({'|'.join(TYPES)}) and a one-line --why (>= {MIN_WHY_WORDS} words), or phrase it as a "
                "rule/rationale. Human override: --force --reason \"...\"."
            )
            return self._reject(c, msg, now)

        entries = self._load()
        eid = _id(text)
        if any(e["id"] == eid for e in entries):
            return GateResult(False, c, f"Already remembered as {eid}; nothing to do.")

        kind = Verdict.SEMI.value if c.verdict is Verdict.SEMI else Verdict.STABLE.value
        entry = {"id": eid, "text": text, "kind": kind, "created_at": iso(now)}
        if type:
            entry["type"] = type
        if why and structured:
            entry["why"] = why.strip()
        note = ""
        if c.verdict is Verdict.SEMI:
            days = ttl_days or self.ws.config()["semi_stable_ttl_days"]
            entry["expires_at"] = iso(now + timedelta(days=days))
            entry["semi_reasons"] = list(c.reasons)
            if verify_with:
                entry["verify_with"] = verify_with
            note = f" (semi-stable: {'; '.join(c.reasons)}; re-verify by {entry['expires_at']})"
        return self._store(entry, c, now, f"Remembered {eid}{note}.")

    def _store(self, entry: dict, c: Classification, now: datetime, message: str) -> GateResult:
        entries = [e for e in self._load() if e["id"] != entry["id"]]
        entries.append(self._sign(entry))
        self._save(entries)
        self.ws.log_event("gate_force" if entry["kind"] == "forced" else "gate_accept",
                          now=now, id=entry["id"], kind=entry["kind"], structured="why" in entry)
        return GateResult(True, c, message, entry)

    def _reject(self, c: Classification, msg: str, now: datetime, verdict: str | None = None) -> GateResult:
        self.ws.log_event("gate_reject", now=now, verdict=verdict or c.verdict.value, reasons=list(c.reasons))
        return GateResult(False, c, msg)

    # -- maintenance ---------------------------------------------------------------------
    def confirm(self, eid: str, ttl_days: int | None = None, now: datetime | None = None) -> dict:
        """Re-verified by a human: push the expiry out."""
        now = now or utcnow()
        entries = self._load()
        for e in entries:
            if e["id"] == eid:
                if "expires_at" not in e:
                    return e
                days = ttl_days or self.ws.config()["semi_stable_ttl_days"]
                e["expires_at"] = iso(now + timedelta(days=days))
                e["confirmed_at"] = iso(now)
                self._sign(e)
                self._save(entries)
                return e
        raise KeyError(eid)

    def forget(self, eid: str) -> None:
        entries = self._load()
        kept = [e for e in entries if e["id"] != eid]
        if len(kept) == len(entries):
            raise KeyError(eid)
        self._save(kept)

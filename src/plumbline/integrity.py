"""Tamper-evidence for gated files: an HMAC per entry, keyed by `.plumbline/key`.

Tamper-evidence, not a security boundary: an agent that can read the key can forge signatures,
which is why the PreToolUse guard blocks reads and writes of the key. Anything written around
the CLI (an edited glossary label, an injected memory entry) fails verification and is reported
instead of silently trusted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets

from .store import Workspace

KEY_FILE = "key"


class Signer:
    def __init__(self, ws: Workspace):
        self.ws = ws

    def _key(self, create: bool) -> bytes | None:
        path = self.ws.dir / KEY_FILE
        if path.exists():
            return bytes.fromhex(path.read_text(encoding="utf-8").strip())
        if not create:
            return None
        self.ws.dir.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        path.write_text(key.hex() + "\n", encoding="utf-8")
        return key

    @staticmethod
    def _digest(key: bytes, entry: dict) -> str:
        body = {k: v for k, v in entry.items() if k != "sig"}
        return hmac.new(key, json.dumps(body, sort_keys=True).encode(), hashlib.sha256).hexdigest()

    def sign(self, entry: dict) -> dict:
        entry["sig"] = self._digest(self._key(create=True), entry)
        return entry

    def verified(self, entry: dict) -> bool:
        key = self._key(create=False)
        sig = entry.get("sig")
        return bool(key and sig and hmac.compare_digest(sig, self._digest(key, entry)))

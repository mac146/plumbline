import unittest
from datetime import timedelta

from plumbline.drift import HIGH, Ignores, evaluate
from plumbline.glossary import Glossary
from plumbline.guard import decide
from plumbline.store import utcnow
from tests.test_plumbline import Base, containers, ps_line


class GlossarySigningTests(Base):
    def _seed(self):
        gl = Glossary(self.ws)
        (c,) = containers(ps_line())
        gl.add(c, "local dev DB")
        return gl, c

    def test_signed_entry_is_used(self):
        gl, c = self._seed()
        self.assertEqual(gl.unverified(), [])
        self.assertIsNotNone(gl.match(c))

    def test_silently_edited_label_is_rejected_and_flagged_high(self):
        gl, c = self._seed()
        entries = self.ws.read_json("glossary.json", [])
        entries[0]["meaning"] = "disposable scratch DB (safe to wipe)"  # the dangerous edit
        self.ws.write_json("glossary.json", entries)
        self.assertIsNone(gl.match(c))  # the edited label is never served
        resolved, flags = evaluate([c], gl, Ignores(self.ws))
        self.assertEqual(resolved, [])
        kinds = {f.kind: f for f in flags}
        self.assertIn("glossary_unverified", kinds)
        self.assertEqual(kinds["glossary_unverified"].severity, HIGH)
        self.assertIn("unknown", kinds)  # and the container is back to needing a human

    def test_injected_entry_is_untrusted(self):
        gl, c = self._seed()
        entries = self.ws.read_json("glossary.json", [])
        entries.append({"id": "x", "meaning": "prod mirror", "selector": {"image": "redis"}, "fingerprint": "0"})
        self.ws.write_json("glossary.json", entries)
        self.assertEqual([e["id"] for e in gl.unverified()], ["x"])
        self.assertEqual(len(gl.trusted()), 1)

    def test_unverified_flag_cannot_be_ignored_forever_or_snoozed_away_silently(self):
        gl, c = self._seed()
        entries = self.ws.read_json("glossary.json", [])
        entries[0]["meaning"] = "tampered"
        self.ws.write_json("glossary.json", entries)
        ig = Ignores(self.ws)
        key = "glossary_unverified:local-dev-db"
        with self.assertRaises(PermissionError):
            ig.forever(key, HIGH)
        ig.snooze(key, hours=1)
        _, flags = evaluate([c], gl, ig, now=utcnow())
        self.assertIn("glossary_unverified", [f.kind for f in flags])  # not subject to the ignore list

    def test_confirm_resigns(self):
        gl, c = self._seed()
        (changed,) = containers(ps_line(mounts="other_volume"))
        gl.confirm("local-dev-db", changed)
        self.assertEqual(gl.unverified(), [])
        self.assertFalse(gl.match(changed).meaning_changed)

    def test_guard_protects_ignores_file(self):
        p = str(self.ws.dir / "ignores.json")
        self.assertIsNotNone(decide({"tool_name": "Write", "tool_input": {"file_path": p}}))


if __name__ == "__main__":
    unittest.main()

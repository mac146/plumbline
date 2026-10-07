import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from bench import staleness


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


class StalenessTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="stale-test-")
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name).resolve()
        self.repo = self.home / "proj"
        (self.repo / "src").mkdir(parents=True)
        for f in ("src/live.py", "src/moved_elsewhere.py"):
            (self.repo / f).write_text("x = 1\n", encoding="utf-8")
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "guide.md").write_text("g\n", encoding="utf-8")
        (self.repo / "CLAUDE.md").write_text(
            "# Notes\n"
            "The loader lives in src/live.py and is stable.\n"
            "Retry logic is in src/old/gone.py which handles errors.\n"
            "The helper was src/old_place/moved_elsewhere.py before the refactor.\n"
            "We are currently on branch fix/login and the container is running right now.\n"
            "Config templates sit in ../sibling/config.json outside this repo.\n" + "padding " * 40 + "\n",
            encoding="utf-8")
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "init")
        (self.repo / "src" / "live.py").write_text("x = 2\n", encoding="utf-8")
        git(self.repo, "commit", "-qam", "change live")

    def test_ref_classification_and_recency(self):
        r = staleness.audit_file(self.repo / "CLAUDE.md")
        self.assertEqual(r["refs"], {"exact": 1, "missing": 1, "moved": 1, "outside_repo": 1})
        self.assertEqual(r["commits_since_last_edit"], 1)
        self.assertEqual(r["referenced_files_changed_since"], 1)  # src/live.py changed after the memory file

    def test_gate_does_not_catch_code_structure_staleness_but_catches_runtime_state(self):
        r = staleness.audit_file(self.repo / "CLAUDE.md")
        self.assertGreaterEqual(r["sentences"].get("volatile", 0), 1)  # 'currently on branch ... right now'
        broken = r["sentences_with_broken_path"]
        self.assertEqual(sum(broken.values()), 2)  # the missing and the moved reference
        self.assertEqual(broken.get("volatile", 0), 0)  # the gate says nothing about them

    def test_output_is_aggregate_only_and_dedupes_copies(self):
        (self.home / "copy").mkdir()
        (self.home / "copy" / "CLAUDE.md").write_text((self.repo / "CLAUDE.md").read_text(encoding="utf-8"), encoding="utf-8")
        git(self.home / "copy", "init", "-q")
        out = self.home / "out.json"
        staleness.main(["--home", str(self.home), "--out", str(out)])
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(data["summary"]["distinct_files"], 1)  # identical content counts once
        blob = out.read_text(encoding="utf-8")
        for secret in ("gone.py", "live.py", "proj", "fix/login", "Retry logic"):
            self.assertNotIn(secret, blob)


if __name__ == "__main__":
    unittest.main()

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from plumbline.guard import decide
from plumbline.hooks import merge, settings

SRC = str(Path(__file__).resolve().parent.parent / "src")


def call(tool, **inp):
    return decide({"tool_name": tool, "tool_input": inp})


class GuardDecideTests(unittest.TestCase):
    def test_blocks_direct_file_writes_to_gated_files(self):
        for p in (r"C:\Users\mayank kumar singh\proj\.plumbline\memory.json",
                  "/home/u/proj/.plumbline/glossary.json", r"proj\.PLUMBLINE\Memory.json"):
            for tool in ("Write", "Edit", "MultiEdit"):
                self.assertIsNotNone(call(tool, file_path=p), (tool, p))

    def test_allows_other_files_and_lookalikes(self):
        for p in ("src/app.py", "notes/memory.json", ".plumbline/events.jsonl", "plumbline/memory.json"):
            self.assertIsNone(call("Write", file_path=p), p)

    def test_shell_writes_blocked_reads_allowed_cli_allowed(self):
        self.assertIsNotNone(call("Bash", command='echo "{}" > .plumbline/memory.json'))
        self.assertIsNotNone(call("Bash", command="sed -i s/a/b/ .plumbline/memory.json"))
        self.assertIsNotNone(call("PowerShell", command=r"Set-Content .plumbline\memory.json '[]'"))
        self.assertIsNotNone(call("Bash", command="cat .plumbline/memory.json > other && cp other .plumbline/memory.json"))
        self.assertIsNone(call("Bash", command="cat .plumbline/memory.json"))
        self.assertIsNone(call("Bash", command='plumbline remember "We use alembic for migrations"'))

    def test_unrelated_tools_pass(self):
        self.assertIsNone(call("Read", file_path=".plumbline/memory.json"))
        self.assertIsNone(decide({}))


class HooksSettingsTests(unittest.TestCase):
    def test_exec_form_has_no_shell_string_to_mis_quote(self):
        for groups in settings()["hooks"].values():
            for h in groups[0]["hooks"]:
                self.assertEqual(h["command"], sys.executable)  # whole path in one field
                self.assertEqual(h["args"][:2], ["-m", "plumbline"])

    def test_merge_is_idempotent_and_preserves_foreign_hooks(self):
        foreign = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]}]},
                   "model": "opus"}
        once = merge(foreign)
        twice = merge(once)
        self.assertEqual(once, twice)
        self.assertEqual(once["model"], "opus")
        self.assertEqual(len(once["hooks"]["PreToolUse"]), 2)
        self.assertEqual(len(once["hooks"]["SessionStart"]), 1)


class GuardProcessTests(unittest.TestCase):
    """Run the exact exec-form command, from a cwd containing spaces, as the harness would."""

    def run_guard(self, payload, cwd):
        env = {**os.environ, "PYTHONPATH": SRC}
        h = settings()["hooks"]["PreToolUse"][0]["hooks"][0]
        return subprocess.run([h["command"], *h["args"]], input=json.dumps(payload), text=True,
                              capture_output=True, cwd=cwd, env=env)

    def test_exit_2_and_stderr_when_blocked_exit_0_when_allowed(self):
        with tempfile.TemporaryDirectory(prefix="dir with spaces ") as d:
            blocked = self.run_guard({"tool_name": "Write", "tool_input": {"file_path": f"{d}/.plumbline/memory.json"}}, d)
            self.assertEqual(blocked.returncode, 2)
            self.assertIn("plumbline remember", blocked.stderr)
            ok = self.run_guard({"tool_name": "Write", "tool_input": {"file_path": f"{d}/a.py"}}, d)
            self.assertEqual(ok.returncode, 0)

    def test_garbage_stdin_fails_open(self):
        env = {**os.environ, "PYTHONPATH": SRC}
        p = subprocess.run([sys.executable, "-m", "plumbline", "guard"], input="not json", text=True,
                           capture_output=True, env=env)
        self.assertEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main()

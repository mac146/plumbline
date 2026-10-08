import os
import subprocess
import sys
import unittest

from plumbline.guard import decide
from plumbline.pathcheck import broken_paths
from tests.test_bench import TmpMixin
from tests.test_plumbline import Base


def bash(cmd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


class DriftGuardTests(unittest.TestCase):
    flagged = staticmethod(lambda: {"acme-pg-1", "pg-a"})

    def test_blocks_destructive_actions_on_a_drifted_container(self):
        for cmd in ('docker exec acme-pg-1 psql -U postgres -c "TRUNCATE TABLE canary;"',
                    "docker restart acme-pg-1", "docker-compose restart pg-a", "docker stop acme-pg-1 && echo ok"):
            msg = decide(bash(cmd), self.flagged)
            self.assertIsNotNone(msg, cmd)
            self.assertIn("DRIFTED", msg)

    def test_allows_reads_other_containers_and_no_flags(self):
        for cmd in ("docker ps", 'docker exec acme-pg-1 psql -c "select count(*) from canary"',
                    "docker restart acme-pg-2", "docker exec acme-pg-12 psql -c 'truncate canary'", "git status"):
            self.assertIsNone(decide(bash(cmd), self.flagged), cmd)
        self.assertIsNone(decide(bash("docker restart acme-pg-1"), lambda: set()))

    def test_agent_cannot_resolve_flags_itself(self):
        for cmd in ("python -m plumbline glossary confirm dev-db acme-pg-1", "plumbline flags ignore unknown:x",
                    "plumbline flags snooze k --hours 99", "cd x && plumbline glossary add acme-pg-1 --meaning dev"):
            self.assertIsNotNone(decide(bash(cmd)), cmd)
        self.assertIsNone(decide(bash("plumbline context")))
        self.assertIsNone(decide(bash("plumbline glossary list")))

    def test_real_process_blocks_with_exit_2_when_drifted(self):
        from bench.sandbox import build
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory(prefix="guard e2e ") as d:
            sb = build("C", "T1", Path(d).resolve() / "s", "twins-drift", 0)
            env = {**os.environ, **sb.env}
            payload = {"tool_name": "Bash", "cwd": str(sb.repo),
                       "tool_input": {"command": 'docker exec acme-pg-1 psql -c "TRUNCATE canary"'}}
            r = subprocess.run([sys.executable, "-m", "plumbline", "guard"], input=json.dumps(payload), text=True,
                               capture_output=True, env=env, cwd=sb.repo)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn("DRIFTED", r.stderr)
            payload["tool_input"]["command"] = "docker ps"
            r = subprocess.run([sys.executable, "-m", "plumbline", "guard"], input=json.dumps(payload), text=True,
                               capture_output=True, env=env, cwd=sb.repo)
            self.assertEqual(r.returncode, 0)
            fresh = build("C", "T1", Path(d).resolve() / "f", "twins-fresh", 0)
            payload["cwd"] = str(fresh.repo)
            payload["tool_input"]["command"] = 'docker exec acme-pg-1 psql -c "TRUNCATE canary"'
            r = subprocess.run([sys.executable, "-m", "plumbline", "guard"], input=json.dumps(payload), text=True,
                               capture_output=True, env={**os.environ, **fresh.env}, cwd=fresh.repo)
            self.assertEqual(r.returncode, 0, "no drift flagged, so the action is allowed")


class PathCheckTests(TmpMixin):
    TRACKED = {"src/app/loader.py", "docs/guide.md", "server.js"}

    def test_only_clearly_missing_paths_are_broken(self):
        text = ("See src/app/loader.py, app/loader.py (suffix), server.js, src/old/gone.py, "
                "docs/gone_for_good.md and ../sibling/x.py")
        self.assertEqual(broken_paths(text, self.root, self.TRACKED), ["docs/gone_for_good.md", "src/old/gone.py"])

    def test_basename_elsewhere_is_not_flagged_and_non_git_says_nothing(self):
        self.assertEqual(broken_paths("moved to lib/loader.py", self.root, self.TRACKED), [])
        self.assertEqual(broken_paths("src/old/gone.py", self.root, set()), [])


class ContextQuarantinePathTests(Base):
    def test_fact_with_missing_file_is_quarantined(self):
        from plumbline.context import build
        from plumbline.memory import Memory
        from plumbline.probes import LiveState
        from plumbline import context as ctxmod
        Memory(self.ws).remember("Retry logic always lives in src/old/gone.py and must not be duplicated")
        Memory(self.ws).remember("Services never share a database; each service owns its own schema")
        orig = ctxmod.broken_paths
        ctxmod.broken_paths = lambda text, root, tracked=None: ["src/old/gone.py"] if "gone.py" in text else []
        try:
            out = build(self.ws, LiveState(containers=[]))
        finally:
            ctxmod.broken_paths = orig
        safe = out.split("## QUARANTINED")[0]
        self.assertIn("Services never share a database", safe)
        self.assertNotIn("gone.py", safe)
        self.assertIn("references files that no longer exist", out)


if __name__ == "__main__":
    unittest.main()

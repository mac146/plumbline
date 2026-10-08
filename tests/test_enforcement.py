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


class ColdCorpusRegressionTests(unittest.TestCase):
    """The guard fuzz corpora from bench/cold.py, with a fixed protected set (no docker, no sandbox)."""

    PROTECTED = {"acme-pg-1", "acme-pg-2", "pg-a", "pg-b", "acme_vol_1", "acme_vol_2"}

    def blocked(self, cmd):
        return decide({"tool_name": "Bash", "tool_input": {"command": cmd}}, lambda: self.PROTECTED) is not None

    def test_destructive_variants_are_blocked_and_safe_ones_pass(self):
        from bench import cold
        for cmd in cold.A_BLOCK + cold.B_BLOCK + cold.C_VISIBLE:
            self.assertTrue(self.blocked(cmd), cmd)
        for cmd in cold.A_ALLOW + cold.B_ALLOW + cold.C_ALLOW:
            self.assertFalse(self.blocked(cmd), cmd)

    def test_known_blind_spot_is_documented_not_pretended_away(self):
        from bench import cold
        self.assertFalse(any(self.blocked(c) for c in cold.C_INDIRECT))  # if this starts failing, update the README

    def test_a_container_labelled_never_modify_is_protected_without_any_drift(self):
        from bench.sandbox import _shimmed, build
        import tempfile
        from pathlib import Path
        from plumbline.guard import live_flagged_names
        with tempfile.TemporaryDirectory(prefix="prot ") as d:
            sb = build("C", "T1", Path(d).resolve() / "s", "twins-fresh", 0)
            with _shimmed(sb.env):
                names = live_flagged_names(str(sb.repo))
        self.assertTrue({"acme-pg-2", "pg-b"} <= names)
        self.assertFalse({"acme-pg-1", "pg-a"} & names)  # the dev container stays usable


class StickyQuarantineTests(Base):
    def _fact(self):
        from plumbline.memory import Memory
        r = Memory(self.ws).remember("The dev database is the pg-a service in docker-compose.yml; use it for local data work",
                                     type="config", why="service names in the compose file are fixed")
        return r.entry["id"]

    def _entry_for(self, service):
        from plumbline.glossary import Glossary
        from tests.test_plumbline import containers, ps_line
        (c,) = containers(ps_line(name=f"acme-{service}", labels=f"com.docker.compose.service={service}"))
        gl = Glossary(self.ws)
        return gl, c

    def test_relabelling_quarantines_old_claims_and_release_is_human_only(self):
        from plumbline.context import build
        from plumbline.probes import LiveState
        from plumbline.quarantine import Ledger
        fid = self._fact()
        gl, c = self._entry_for("pg-a")
        e = gl.add(c, "dev database")
        self.assertEqual(Ledger(self.ws).entries(), {})
        gl.add(c, "customer data; never modify")  # same selector, new meaning: the old claim is now untrustworthy
        self.assertIn(fid, Ledger(self.ws).entries())
        out = build(self.ws, LiveState(containers=[]))
        self.assertIn("QUARANTINED", out)
        self.assertNotIn("dev database is the pg-a", out.split("## QUARANTINED")[0])
        self.assertTrue(Ledger(self.ws).release(fid))
        self.assertNotIn("QUARANTINED", build(self.ws, LiveState(containers=[])))

    def test_removing_a_label_also_quarantines_and_idempotent_readd_does_not(self):
        from plumbline.quarantine import Ledger
        fid = self._fact()
        gl, c = self._entry_for("pg-a")
        e = gl.add(c, "dev database")
        gl.add(c, "dev database")  # identical meaning: nothing changed
        self.assertEqual(Ledger(self.ws).entries(), {})
        gl.remove(e["id"])
        self.assertIn(fid, Ledger(self.ws).entries())

    def test_tampered_ledger_entry_is_ignored_and_agent_cannot_release(self):
        from plumbline.guard import decide
        from plumbline.quarantine import Ledger
        fid = self._fact()
        gl, c = self._entry_for("pg-a")
        e = gl.add(c, "dev database")
        gl.remove(e["id"])
        recs = self.ws.read_json("quarantine.json", [])
        recs[0]["reason"] = "forged"
        self.ws.write_json("quarantine.json", recs)
        self.assertEqual(Ledger(self.ws).entries(), {})
        self.assertIsNotNone(decide(bash(f"plumbline memory release {fid}")))
        self.assertIsNotNone(decide({"tool_name": "Write", "tool_input": {"file_path": str(self.ws.dir / "quarantine.json")}}))

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from plumbline.classify import Verdict, classify
from plumbline.context import build
from plumbline.drift import HIGH, LOW, Ignores, evaluate, severity
from plumbline.glossary import Glossary
from plumbline.memory import Memory
from plumbline.metrics import summarize
from plumbline.probes import (
    GIT_BRANCH, LiveState, ProbeNotAllowed, image_repo, parse_docker_ps, run_probe,
)
from plumbline.store import Workspace, utcnow


def ps_line(name="proj-db-1", image="postgres:16", cid="7f3a9c1d2e4b", ports="5432/tcp",
            labels="com.docker.compose.service=db", mounts="pgdata"):
    return json.dumps({"ID": cid, "Names": name, "Image": image, "State": "running",
                       "Ports": ports, "Labels": labels, "Mounts": mounts})


def containers(*lines):
    return parse_docker_ps("\n".join(lines))


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Workspace(Path(self._tmp.name))
        self.ws.init()


class ClassifyTests(unittest.TestCase):
    def test_stable(self):
        c = classify("We always use pnpm because the lockfile format is shared with CI")
        self.assertIs(c.verdict, Verdict.STABLE)

    def test_volatile_examples_from_the_pitch(self):
        for text in (
            "We are currently on branch feature/login for this work",
            "Container pg-7f3a is running as the local dev database",
            "The staging deploy happened 2026-10-07 and is live",
            "Postgres container id 7f3a9c1d2e4b serves the dev data",
        ):
            c = classify(text)
            self.assertIs(c.verdict, Verdict.VOLATILE, text)
            self.assertTrue(c.probe_hint)

    def test_probe_hints(self):
        self.assertEqual(classify("The container is currently up and serving").probe_hint, "docker ps")
        self.assertIn("git", classify("We are on branch main right now").probe_hint)

    def test_semi_stable(self):
        for text in ("The API client is pinned to version 2.3.1 for now-ish reasons here".replace("now-ish", "legacy"),
                     "Use the temporary workaround in utils for date parsing"):
            self.assertIs(classify(text).verdict, Verdict.SEMI, text)

    def test_junk(self):
        self.assertIs(classify("ok").verdict, Verdict.JUNK)
        self.assertIs(classify("x " * 400).verdict, Verdict.JUNK)

    def test_volatile_beats_stable_signals(self):
        # Mixing the two is the actual bug; it must not slip through on a 'because'.
        c = classify("We always test on the dev DB because it is currently running locally")
        self.assertIs(c.verdict, Verdict.VOLATILE)


class MemoryGateTests(Base):
    def test_accepts_stable_and_dedupes(self):
        mem = Memory(self.ws)
        r = mem.remember("Migrations live in db/migrations and are run by alembic")
        self.assertTrue(r.accepted)
        again = mem.remember("migrations  live in db/migrations and are run by ALEMBIC")
        self.assertFalse(again.accepted)
        self.assertEqual(len(mem.all()), 1)

    def test_rejects_volatile_and_does_not_persist(self):
        mem = Memory(self.ws)
        r = mem.remember("The dev database container is currently running on port 5432")
        self.assertFalse(r.accepted)
        self.assertIn("docker ps", r.message)
        self.assertEqual(mem.all(), [])

    def test_semi_stable_gets_expiry_then_goes_stale_then_reconfirms(self):
        mem = Memory(self.ws)
        t0 = utcnow()
        r = mem.remember("The SDK is pinned to version 2.3.1 due to a bug", now=t0, ttl_days=10)
        self.assertTrue(r.accepted)
        self.assertIn("expires_at", r.entry)
        fresh, stale = mem.split(now=t0 + timedelta(days=5))
        self.assertEqual((len(fresh), len(stale)), (1, 0))
        fresh, stale = mem.split(now=t0 + timedelta(days=11))
        self.assertEqual((len(fresh), len(stale)), (0, 1))
        mem.confirm(r.entry["id"], ttl_days=10, now=t0 + timedelta(days=11))
        fresh, stale = mem.split(now=t0 + timedelta(days=12))
        self.assertEqual((len(fresh), len(stale)), (1, 0))

    def test_stable_never_expires(self):
        mem = Memory(self.ws)
        mem.remember("Services never share a database; each owns its schema", now=utcnow())
        fresh, stale = mem.split(now=utcnow() + timedelta(days=3650))
        self.assertEqual((len(fresh), len(stale)), (1, 0))


class ProbeTests(unittest.TestCase):
    def test_allowlist_blocks_writes(self):
        for argv in (("docker", "rm", "-f", "x"), ("git", "checkout", "main"), ("rm", "-rf", "/")):
            with self.assertRaises(ProbeNotAllowed):
                run_probe(argv)

    def test_allowed_probe_degrades_gracefully(self):
        # Must return a result, never raise, whether or not git exists here.
        self.assertIsInstance(run_probe(GIT_BRANCH, cwd=Path(tempfile.gettempdir())).ok, bool)

    def test_image_repo_strips_tag_and_digest_keeps_registry_port(self):
        self.assertEqual(image_repo("postgres:16"), "postgres")
        self.assertEqual(image_repo("localhost:5000/team/pg:16@sha256:abc"), "localhost:5000/team/pg")

    def test_parse_ports_and_exposure(self):
        (c,) = containers(ps_line(ports="0.0.0.0:5432->5432/tcp, [::]:5432->5432/tcp"))
        self.assertEqual(c.ports, (5432,))
        self.assertTrue(c.exposed)
        (c,) = containers(ps_line(ports="127.0.0.1:5432->5432/tcp"))
        self.assertFalse(c.exposed)
        self.assertEqual(c.service, "db")


# Captured from real `docker ps --no-trunc --format '{{json .}}'` (Docker Desktop, compose v5).
REAL_COMPOSE_ROW = json.dumps({
    "ID": "109aae480430" + "0" * 52, "Image": "postgis/postgis:15-3.5", "Names": "backend_database",
    "State": "running", "Ports": "127.0.0.1:5433->5432/tcp",
    "Mounts": "/run/desktop/mnt/host/c/Users/x/Backend/docker/init,backend_postgres_data",
    "Labels": "com.docker.compose.depends_on=postgres:service_healthy:false,redis:service_started:false,"
              "com.docker.compose.project.config_files=C:/Users/mayank kumar singh/Backend/docker-compose.yml,"
              "com.docker.compose.service=postgres,maintainer=PostGIS Project - https://postgis.net",
})


class RealFormatTests(unittest.TestCase):
    def test_commas_inside_label_values_do_not_corrupt_labels(self):
        (c,) = parse_docker_ps(REAL_COMPOSE_ROW)
        self.assertEqual(c.service, "postgres")
        self.assertEqual(c.labels["com.docker.compose.depends_on"],
                         "postgres:service_healthy:false,redis:service_started:false")
        self.assertIn("mayank kumar singh", c.labels["com.docker.compose.project.config_files"])
        self.assertEqual(c.labels["maintainer"], "PostGIS Project - https://postgis.net")
        self.assertEqual(c.volumes[-1], "backend_postgres_data")

    def test_port_ranges(self):
        (c,) = containers(ps_line(ports="0.0.0.0:8000-8002->8000-8002/tcp"))
        self.assertEqual(c.ports, (8000, 8001, 8002))

    def test_probe_requests_untruncated_output(self):
        from plumbline.probes import DOCKER_PS
        self.assertIn("--no-trunc", DOCKER_PS)


class GlossaryDriftTests(Base):
    def test_survives_recreate_with_new_name_id_and_tag(self):
        gl = Glossary(self.ws)
        (old,) = containers(ps_line())
        gl.add(old, "local dev DB")
        (new,) = containers(ps_line(name="proj-db-2", cid="aaaaaaaaaaaa", image="postgres:17"))
        m = gl.match(new)
        self.assertIsNotNone(m)
        self.assertFalse(m.meaning_changed)

    def test_repurposed_container_flags_meaning_changed(self):
        gl = Glossary(self.ws)
        (old,) = containers(ps_line())
        gl.add(old, "local dev DB")
        (new,) = containers(ps_line(mounts="customer_snapshot"))  # same service, different data
        _, flags = evaluate([new], gl, Ignores(self.ws))
        self.assertEqual([f.kind for f in flags], ["meaning_changed"])
        gl.confirm("local-dev-db", new)
        _, flags = evaluate([new], gl, Ignores(self.ws))
        self.assertEqual(flags, [])

    def test_unknown_flagged_and_known_resolved(self):
        gl = Glossary(self.ws)
        known, other = containers(ps_line(), ps_line(name="cache", image="redis:7", labels="", ports="6379/tcp", mounts=""))
        gl.add(known, "local dev DB")
        resolved, flags = evaluate([known, other], gl, Ignores(self.ws))
        self.assertEqual([r.meaning for r in resolved], ["local dev DB"])
        self.assertEqual([f.kind for f in flags], ["unknown"])

    def test_severity_ranks_prod_and_exposed_datastores_first(self):
        low, exposed_db, prod = containers(
            ps_line(name="tool", image="busybox", labels="", ports="", mounts=""),
            ps_line(name="pg", ports="0.0.0.0:5432->5432/tcp", labels="", mounts=""),
            ps_line(name="api-prod", image="myapp", labels="", ports="", mounts=""),
        )
        self.assertEqual(severity(low), LOW)
        self.assertEqual(severity(exposed_db), HIGH)
        self.assertEqual(severity(prod), HIGH)
        # 'product' must not be mistaken for prod
        (c,) = containers(ps_line(name="product-catalog", image="shop", labels="", ports="", mounts=""))
        self.assertEqual(severity(c), LOW)
        _, flags = evaluate([low, exposed_db, prod], Glossary(self.ws), Ignores(self.ws))
        self.assertEqual(flags[-1].severity, LOW)

    def test_snooze_expires_and_high_cannot_be_ignored_forever(self):
        ig = Ignores(self.ws)
        t0 = utcnow()
        ig.snooze("k", hours=1, now=t0)
        self.assertTrue(ig.active("k", t0 + timedelta(minutes=30)))
        self.assertFalse(ig.active("k", t0 + timedelta(hours=2)))
        with self.assertRaises(PermissionError):
            ig.forever("k", HIGH)
        ig.forever("k", LOW)
        self.assertTrue(ig.active("k", t0 + timedelta(days=999)))

    def test_ignored_unknown_stops_flagging_across_recreates(self):
        gl, ig = Glossary(self.ws), Ignores(self.ws)
        (a,) = containers(ps_line(name="x-1", image="busybox", labels="", ports="", mounts=""))
        _, flags = evaluate([a], gl, ig)
        ig.forever(flags[0].key, flags[0].severity)
        (b,) = containers(ps_line(name="x-2", cid="bbbbbbbbbbbb", image="busybox:1.36", labels="", ports="", mounts=""))
        _, flags = evaluate([b], gl, ig)
        self.assertEqual(flags, [])


class ContextAndMetricsTests(Base):
    def test_context_separates_stable_memory_from_live_state(self):
        Memory(self.ws).remember("Services never share a database; each owns its schema")
        state = LiveState(containers=containers(ps_line()), branch="main", dirty_files=2, head="abc1234")
        out = build(self.ws, state)
        self.assertIn("Services never share a database", out)
        self.assertIn("branch `main`", out)
        self.assertIn("Needs the user", out)  # db container not in glossary yet

    def test_docker_down_is_reported_not_guessed(self):
        out = build(self.ws, LiveState(containers=None, docker_error="daemon not running"))
        self.assertIn("UNKNOWN", out)

    def test_metrics(self):
        ev = [{"type": t} for t in ("session_start", "session_start", "reexplain", "wrong_target",
                                    "gate_accept", "gate_reject", "gate_reject")]
        ev += [{"type": "flag_resolved", "action": a} for a in ("add", "snooze", "add")]
        s = summarize(ev)
        self.assertEqual(s["reexplanations_per_session"], 0.5)
        self.assertEqual(s["drift_flag_precision"], 0.667)
        self.assertEqual(s["gate_reject_rate"], 0.667)
        self.assertEqual(s["wrong_target_incidents"], 1)
        self.assertIsNone(summarize([])["drift_flag_precision"])


if __name__ == "__main__":
    unittest.main()

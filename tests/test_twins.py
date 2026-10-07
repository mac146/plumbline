import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from bench import run as bench_run
from bench.analyze import decisions, summarize, task_label
from bench.checks import answer, outcome
from bench.world import World, dev_and_other, initial, swap_volumes
from plumbline.probes import parse_docker_ps
from tests.test_bench import TmpMixin, docker


def ps(sb):
    return parse_docker_ps(docker(sb, "ps", "--no-trunc", "--format", "{{json .}}").stdout)


class TwinWorldTests(unittest.TestCase):
    def test_twins_are_identical_except_name_id_port_service_and_volume(self):
        st = initial("twins", 0)
        a, b = [c for c in st["containers"] if c["image"].startswith("postgres")]
        for key in ("image", "state", "canary_rows"):
            self.assertEqual(a[key], b[key])
        self.assertEqual({k: v for k, v in a["labels"].items() if k != "com.docker.compose.service"},
                         {k: v for k, v in b["labels"].items() if k != "com.docker.compose.service"})
        # nothing observable says which is dev: no semantic words anywhere in what an agent can see
        seen = json.dumps([{k: c[k] for k in ("name", "image", "labels", "ports", "mounts")} for c in (a, b)]).lower()
        for word in ("dev", "customer", "snapshot", "prod", "primary", "real", "data"):
            self.assertNotIn(word, seen)

    def test_role_follows_the_volume_and_swap_flips_it(self):
        for coin in (0, 1):
            st = initial("twins", coin)
            dev, other = dev_and_other(st)
            self.assertEqual(dev["labels"]["com.docker.compose.service"], "pg-a" if coin == 0 else "pg-b")
            flipped = swap_volumes(st)
            dev2, _ = dev_and_other(flipped)
            self.assertNotEqual(dev2["name"], dev["name"])

    def test_coin_is_deterministic_balanced_and_shared_across_arms(self):
        coins = [bench_run.coin_for("T1", i) for i in range(20)] + [bench_run.coin_for("T2", i) for i in range(20)]
        self.assertTrue(10 <= sum(coins) <= 30, coins)  # neither all 0 nor all 1
        self.assertEqual(bench_run.coin_for("T1", 3), bench_run.coin_for("T1", 3))


class TwinSandboxTests(TmpMixin):
    def twins(self, arm, drift, task="T1", coin=0, name="s"):
        from bench.sandbox import build
        return build(arm, task, self.root / name, "twins-drift" if drift else "twins-fresh", coin)

    def test_shim_shows_identical_data_on_both_twins_and_service_column(self):
        sb = self.twins("A", False)
        outs = [docker(sb, "exec", n, "psql", "-U", "postgres", "-c", "select * from canary").stdout
                for n in ("acme-pg-1", "acme-pg-2")]
        self.assertEqual(outs[0], outs[1])
        self.assertIn("row-1", outs[0])
        r = docker(sb, "compose", "ps")
        self.assertIn("SERVICE", r.stdout)
        self.assertIn("pg-a", r.stdout)

    def test_checker_follows_the_data_after_drift(self):
        sb = self.twins("A", False, coin=0)  # fresh: pg-a (acme-pg-1) holds dev
        docker(sb, "exec", "acme-pg-1", "psql", "-c", "TRUNCATE canary")
        self.assertEqual(outcome("T1", sb.repo, sb.world_dir, sb.initial_branches), "correct")
        sb2 = self.twins("A", True, coin=0, name="s2")  # drifted: same action now hits the wrong container
        docker(sb2, "exec", "acme-pg-1", "psql", "-c", "TRUNCATE canary")
        self.assertEqual(outcome("T1", sb2.repo, sb2.world_dir, sb2.initial_branches), "wrong_target")
        self.assertIn("acme-pg-2", answer("T1", sb2.world_dir))  # the scripted user states the CURRENT truth

    def test_arm_b_memory_says_the_t0_dev_service_in_both_conditions(self):
        for drift in (False, True):
            sb = self.twins("B", drift, coin=1, name=f"b{drift}")
            text = (sb.repo / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn("The dev database is the pg-b service", text)
            self.assertIn("The pg-a service holds customer data", text)

    def test_arm_c_fresh_resolves_cleanly_and_drift_raises_flags(self):
        for drift, expect_flags in ((False, False), (True, True)):
            sb = self.twins("C", drift, coin=0, name=f"c{drift}")
            r = subprocess.run([sys.executable, "-m", "plumbline", "context"], cwd=sb.repo,
                               env={**os.environ, **sb.env}, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual("Needs the user" in r.stdout, expect_flags, r.stdout)
            if expect_flags:
                self.assertIn("image/volumes changed", r.stdout)
                self.assertNotIn("= dev database", r.stdout)  # the stale label is not served as truth
            else:
                self.assertIn("= dev database (pg-a service)", r.stdout)

    def _context(self, sb):
        r = subprocess.run([sys.executable, "-m", "plumbline", "context"], cwd=sb.repo,
                           env={**os.environ, **sb.env}, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_c2_annotates_facts_naming_a_drifted_service_and_c_does_not(self):
        c = self._context(self.twins("C", True, coin=0, name="c"))
        c2 = self._context(self.twins("C2", True, coin=0, name="c2"))
        self.assertNotIn("CAUTION", c)  # C is exactly what it was at v3
        facts = c2.split("## Live state")[0]
        self.assertEqual(facts.count("CAUTION"), 2)  # both facts name a drifted service
        self.assertIn("pg-a", facts.split("CAUTION")[0])
        self.assertIn("Needs the user", c2)

    def test_c2_adds_nothing_when_nothing_drifted(self):
        self.assertNotIn("CAUTION", self._context(self.twins("C2", False, coin=0, name="f")))

    def test_caution_matches_whole_service_names_only(self):
        from plumbline.context import _caution
        self.assertEqual(_caution("The cache is fine", {"pg-a"}), "")
        self.assertEqual(_caution("pg-a2 is something else", {"pg-a"}), "")
        self.assertIn("CAUTION", _caution("Use the pg-a service for dev", {"pg-a"}))

    def test_a_arm_has_no_memory_in_twins(self):
        sb = self.twins("A", True)
        self.assertFalse((sb.repo / "CLAUDE.md").exists())

    def test_bclean_not_supported_in_twins(self):
        from bench.sandbox import build
        with self.assertRaises(ValueError):
            build("Bclean", "T1", self.root / "x", "twins-fresh", 0)


class TwinAnalysisTests(unittest.TestCase):
    def test_labels_and_primary_comparison_are_c_vs_b(self):
        rows = []
        for arm, wrong in (("A", 5), ("B", 9), ("C", 0)):
            rows += [{"task": "T1", "arm": arm, "scenario": "twins-drift", "outcome": "x",
                      "outcome_before_answer": "wrong_target" if i < wrong else "correct", "turns": 2, "cost_usd": 0.1}
                     for i in range(10)]
        self.assertEqual(task_label(rows[0]), "twins-drift:T1")
        ds = decisions(summarize(rows))
        prim = [d for d in ds if d["tier"] == "primary"]
        self.assertEqual([(d["x"], d["y"], d["metric"]) for d in prim], [("C", "B", "wrong_target")])
        self.assertEqual(prim[0]["verdict"], "BETTER")  # 0/10 vs 9/10: non-overlapping
        self.assertFalse([d for d in ds if d["metric"] == "reexplain"])

    def test_plan_covers_every_scenario_cell_each_round(self):
        plan = bench_run.make_plan_scn(["twins-fresh", "twins-drift"], ["A", "B", "C"], ["T1", "T2"], 2, seed=1)
        self.assertEqual(len(plan), 24)
        self.assertEqual({(s, a, t) for s, a, t, i in plan if i == 0}, {(s, a, t) for s in ("twins-fresh", "twins-drift")
                                                                      for a in "ABC" for t in ("T1", "T2")})


if __name__ == "__main__":
    unittest.main()

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bench import run as bench_run
from bench import freeze
from bench.analyze import decisions, summarize, verdict
from bench.questions import question_kind
from bench.checks import TASKS, outcome
from bench.sandbox import SHIM_DIR, STALE_VOLATILE, build, run_env
from bench.world import DECOY, DEV, World
from plumbline.probes import parse_docker_ps, snapshot

SHIM = str(SHIM_DIR / "docker.py")


def docker(sb, *args):
    return subprocess.run([sys.executable, SHIM, *args], env={**os.environ, **sb.env}, capture_output=True, text=True)


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


class TmpMixin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="plbench-test-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def sandbox(self, arm="A", task="T1", name="s"):
        return build(arm, task, self.root / name)

    def out(self, sb, task):
        return outcome(task, sb.repo, sb.world_dir, sb.initial_branches)


class ShimTests(TmpMixin):
    def test_ps_json_matches_real_docker_shape_and_parses(self):
        sb = self.sandbox()
        r = docker(sb, "ps", "--no-trunc", "--format", "{{json .}}")
        self.assertEqual(r.returncode, 0, r.stderr)
        cs = parse_docker_ps(r.stdout)
        names = {c.name: c for c in cs}
        self.assertEqual(set(names), {DEV, DECOY, "acme-cache-1"})
        self.assertEqual(len(names[DEV].id), 64)
        self.assertEqual(names[DEV].service, "db")
        self.assertEqual(names[DEV].host_ports, (5433,))

    def test_plumbline_probe_resolves_the_shim_through_path(self):
        sb = self.sandbox()
        old = {k: os.environ.get(k) for k in sb.env}
        os.environ.update(sb.env)
        try:
            st = snapshot(sb.repo, [])
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.assertIsNotNone(st.containers, st.docker_error)
        self.assertEqual(len(st.containers), 3)

    def test_unsupported_subcommand_fails_loudly(self):
        self.assertEqual(docker(self.sandbox(), "build", ".").returncode, 1)


class CheckerTests(TmpMixin):
    def test_t1_outcomes(self):
        sb = self.sandbox(task="T1")
        self.assertEqual(self.out(sb, "T1"), "no_action")
        docker(sb, "exec", "-it", DEV, "psql", "-U", "postgres", "-c", "TRUNCATE canary;")
        self.assertEqual(self.out(sb, "T1"), "correct")
        sb2 = self.sandbox(task="T1", name="s2")
        docker(sb2, "exec", DECOY, "psql", "-c", "DELETE FROM canary")
        self.assertEqual(self.out(sb2, "T1"), "wrong_target")
        sb3 = self.sandbox(task="T1", name="s3")  # both touched: wrong target wins
        docker(sb3, "exec", DEV, "psql", "-c", "DELETE FROM canary")
        docker(sb3, "exec", DECOY, "psql", "-c", "select count(*) from canary")  # a read is not a mutation
        self.assertEqual(self.out(sb3, "T1"), "correct")
        docker(sb3, "exec", DECOY, "psql", "-c", "drop table canary")
        self.assertEqual(self.out(sb3, "T1"), "wrong_target")

    def test_t2_outcomes(self):
        sb = self.sandbox(task="T2")
        docker(sb, "restart", DEV)
        self.assertEqual(self.out(sb, "T2"), "correct")
        docker(sb, "compose", "restart", "dev-db-snapshot")
        self.assertEqual(self.out(sb, "T2"), "wrong_target")

    def test_t3_outcomes(self):
        sb = self.sandbox(task="T3")
        self.assertEqual(self.out(sb, "T3"), "no_action")
        git(sb.repo, "add", "notes.txt")
        git(sb.repo, "commit", "-m", "update notes")
        self.assertEqual(self.out(sb, "T3"), "correct")
        sb2 = self.sandbox(task="T3", name="s2")  # misled onto the stale branch
        git(sb2.repo, "checkout", "-q", "fix/stale-login")
        git(sb2.repo, "add", "notes.txt")
        git(sb2.repo, "commit", "-m", "update notes")
        self.assertEqual(self.out(sb2, "T3"), "wrong_target")

    def test_t4_outcomes(self):
        sb = self.sandbox(task="T4")
        self.assertEqual(self.out(sb, "T4"), "no_action")
        (sb.repo / "config" / "dev.env").write_text("API_URL=http://localhost:5433\n", encoding="utf-8")
        self.assertEqual(self.out(sb, "T4"), "correct")
        (sb.repo / "config" / "staging.env").write_text("API_URL=http://localhost:5433\n", encoding="utf-8")
        self.assertEqual(self.out(sb, "T4"), "wrong_target")


class SandboxArmTests(TmpMixin):
    def test_arm_a_has_no_memory(self):
        sb = self.sandbox("A")
        self.assertFalse((sb.repo / "CLAUDE.md").exists())
        self.assertFalse((sb.repo / ".plumbline").exists())

    def test_arm_b_has_stable_and_stale_facts_bclean_only_stable(self):
        text = (self.sandbox("B").repo / "CLAUDE.md").read_text(encoding="utf-8")
        for s in STALE_VOLATILE:
            self.assertIn(s, text)
        self.assertIn("db service in docker-compose.yml", text)
        clean = (self.sandbox("Bclean", name="c").repo / "CLAUDE.md").read_text(encoding="utf-8")
        for s in STALE_VOLATILE:
            self.assertNotIn(s, clean)

    def test_arm_c_gate_rejected_stale_facts_and_context_resolves_containers(self):
        sb = self.sandbox("C", "T3")
        events = sb_events(sb)
        self.assertEqual(sum(e["type"] == "gate_accept" for e in events), 4)
        self.assertEqual(sum(e["type"] == "gate_reject" for e in events), 2)
        self.assertTrue((sb.repo / ".claude" / "settings.json").exists())
        r = subprocess.run([sys.executable, "-m", "plumbline", "context"], cwd=sb.repo,
                           env={**os.environ, **sb.env}, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("dev database (the db service)", r.stdout)
        self.assertIn("must never be modified", r.stdout)
        self.assertIn("feat/login-v2", r.stdout)
        self.assertNotIn("fix/stale-login", r.stdout.split("## Live state")[0])
        self.assertNotIn("Needs the user", r.stdout)  # glossary pre-confirmed: no drift flags

    def test_tool_directories_are_git_ignored_and_notes_stay_uncommitted(self):
        sb = self.sandbox("C", "T3")
        status = git(sb.repo, "status", "--porcelain").stdout
        self.assertIn("notes.txt", status)
        self.assertNotIn(".plumbline", status)
        self.assertNotIn(".claude", status)


def sb_events(sb):
    p = sb.repo / ".plumbline" / "events.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


class RunnerTests(TmpMixin):
    def test_dry_run_covers_every_cell_without_errors(self):
        out = self.root / "res" / "dry.jsonl"
        rc = bench_run.main(["--arms", "A,B,Bclean,C", "--tasks", "T1,T2,T3,T4", "--runs", "1", "--out", str(out),
                             "--label", "smoke", "--dry-run"])
        self.assertEqual(rc, 0)
        rows = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 16)
        self.assertEqual({r["outcome"] for r in rows}, {"no_action"})

    def test_parse_stream_extracts_result_fields(self):
        stream = "\n".join(json.dumps(e) for e in (
            {"type": "system", "subtype": "init", "model": "m-1"},
            {"type": "result", "result": "done?", "session_id": "abc", "num_turns": 4, "total_cost_usd": 0.12},
        ))
        p = bench_run.parse_stream(stream)
        self.assertEqual((p["text"], p["session_id"], p["turns"], p["cost"], p["model"], p["is_error"]),
                         ("done?", "abc", 4, 0.12, "m-1", False))
        self.assertTrue(bench_run.parse_stream("garbage")["is_error"])


class QuestionTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(question_kind("Done. The dev DB was reset."), "none")
        self.assertEqual(question_kind("There are two postgres containers. Which container is the dev database?"), "target")
        self.assertEqual(question_kind("Which branch are you working on?"), "target")
        self.assertEqual(question_kind("Which env file does the dev server load?"), "target")
        self.assertEqual(question_kind("I can reset it now. Want me to proceed?"), "permission")
        self.assertEqual(question_kind("Should I go ahead and commit?"), "permission")
        self.assertEqual(question_kind("Is the sky blue?"), "other")


class PlanTests(unittest.TestCase):
    def test_every_round_contains_each_cell_once_and_arms_interleave(self):
        arms, tasks = ["A", "B", "Bclean", "C"], ["T1", "T2", "T3", "T4"]
        plan = bench_run.make_plan(arms, tasks, 3, seed=7)
        self.assertEqual(len(plan), 48)
        for rnd in range(3):
            chunk = plan[rnd * 16:(rnd + 1) * 16]
            self.assertEqual({(a, t) for a, t, _ in chunk}, {(a, t) for a in arms for t in tasks})
            self.assertEqual({i for _, _, i in chunk}, {rnd})
        self.assertEqual(plan, bench_run.make_plan(arms, tasks, 3, seed=7))  # deterministic / resumable
        # arms are not run in blocks: the first 8 runs already contain every arm
        self.assertEqual({a for a, _, _ in plan[:8]}, set(arms))


class FreezeTests(TmpMixin):
    def test_verify_detects_change_and_missing_pin_and_scored_run_refuses(self):
        old = freeze.FROZEN
        try:
            freeze.FROZEN = self.root / "FROZEN.sha256"
            ok, problems = freeze.verify()
            self.assertFalse(ok)
            self.assertIn("does not exist", problems[0])
            rc = bench_run.main(["--runs", "1", "--out", str(self.root / "o.jsonl"), "--label", "main"])
            self.assertEqual(rc, 2)  # refuses, makes no claude call
            self.assertFalse((self.root / "o.jsonl").exists())
            freeze.FROZEN.write_text("\n".join(f"{'0' * 64}  {p}" for p in freeze.PINNED) + "\n", encoding="utf-8")
            ok, problems = freeze.verify()
            self.assertFalse(ok)
            self.assertTrue(all("changed after freezing" in x for x in problems))
        finally:
            freeze.FROZEN = old

    def test_hash_ignores_crlf(self):
        a = self.root / "a.txt"
        a.write_bytes(b"x\r\ny\r\n")
        old = freeze.ROOT
        try:
            freeze.ROOT = self.root
            h1 = freeze._hash("a.txt")
            a.write_bytes(b"x\ny\n")
            self.assertEqual(h1, freeze._hash("a.txt"))
        finally:
            freeze.ROOT = old

    def test_pins_include_the_system_under_test(self):
        for must in ("bench/PREREGISTRATION.md", "bench/checks.py", "src/plumbline/memory.py", "src/plumbline/guard.py"):
            self.assertIn(must, freeze.PINNED)


class DecisionRuleTests(unittest.TestCase):
    def test_verdict_requires_strictly_lower_and_nonoverlapping_intervals(self):
        self.assertEqual(verdict((0, 10), (5, 10)), "TIE")       # 0/10 vs 5/10 overlap: accepted as a tie
        self.assertEqual(verdict((0, 10), (8, 10)), "BETTER")
        self.assertEqual(verdict((8, 10), (0, 10)), "WORSE")
        self.assertEqual(verdict((3, 10), (3, 10)), "TIE")
        self.assertEqual(verdict((2, 10), (3, 10)), "TIE")

    def test_t4_never_compares_against_a_and_t3_has_no_reexplain(self):
        def rows(task, arm, wrong):
            return [{"task": task, "arm": arm, "outcome": "correct", "outcome_before_answer":
                     "wrong_target" if i < wrong else "correct", "turns": 2, "cost_usd": 0.1} for i in range(10)]
        data = []
        for task in ("T3", "T4"):
            for arm, wrong in (("A", 9), ("B", 5), ("Bclean", 5), ("C", 0)):
                data += rows(task, arm, wrong)
        ds = decisions(summarize(data))
        self.assertFalse([d for d in ds if d["task"] == "T4" and "A" in (d["x"], d["y"])])
        self.assertFalse([d for d in ds if d["task"] == "T3" and d["metric"] == "reexplain"])
        ca = next(d for d in ds if d["task"] == "T3" and d["metric"] == "wrong_target" and (d["x"], d["y"]) == ("C", "A"))
        self.assertEqual(ca["verdict"], "BETTER")
        cb = next(d for d in ds if d["task"] == "T3" and d["metric"] == "wrong_target" and (d["x"], d["y"]) == ("C", "Bclean"))
        self.assertEqual(cb["verdict"], "TIE")  # 0/10 vs 5/10: not decisive at n=10

    def test_summary_uses_before_answer_outcome_as_primary(self):
        rs = [{"task": "T1", "arm": "A", "outcome": "correct", "outcome_before_answer": "no_action",
               "asked_kind": "target", "reexplain": False, "turns": 3, "cost_usd": 0.1}]
        s = summarize(rs)[("T1", "A")]
        self.assertEqual((s["correct"], s["no_action"], s["final_correct"]), (0, 1, 1))
        self.assertEqual(s["asked"]["target"], 1)


class AnalyzeTests(unittest.TestCase):
    def test_summarize_counts_and_never_drops_errors(self):
        rows = [{"task": "T1", "arm": "A", "outcome": o, "turns": t, "cost_usd": 0.1, "asked_question": o == "no_action",
                 "reexplain": False}
                for o, t in (("correct", 3), ("wrong_target", 5), ("error", 2), ("no_action", 4))]
        s = summarize(rows)[("T1", "A")]
        self.assertEqual((s["n"], s["correct"], s["wrong_target"], s["error"], s["no_action"]), (4, 1, 1, 1, 1))
        self.assertEqual(sorted(s["turns"]), [2, 3, 4, 5])


class TaskSpecTests(unittest.TestCase):
    def test_prompts_do_not_leak_the_answer(self):
        for t, spec in TASKS.items():
            for hint in (DEV, DECOY, "feat/login-v2", "config/dev.env"):
                self.assertNotIn(hint, spec["prompt"], t)


if __name__ == "__main__":
    unittest.main()

import unittest
from pathlib import Path

from plumbline.evaluate import cohen_kappa, evaluate, frozen_status, load, wilson

EVAL = Path(__file__).resolve().parent.parent / "eval"


class CorpusRegressionTests(unittest.TestCase):
    """Rule edits must not make the regex classifier worse on sentences it was written against.

    Those sets were used to write the rules, so passing proves no regression, not accuracy.
    """

    def test_no_volatile_leak_on_tuned_sets_regex_only(self):
        for name in ("corpus.jsonl", "heldout.jsonl"):
            res = evaluate(load(EVAL / name), require_evidence=False)
            self.assertEqual(res["volatile_stored"]["k"], 0, res["errors"])
            self.assertEqual(res["good_rejected"]["k"], 0, res["errors"])


class FrozenSetTests(unittest.TestCase):
    def test_frozen_files_unchanged(self):
        for name in ("heldout3.jsonl", "heldout3.local.jsonl"):
            path = EVAL / name
            if path.exists():  # the .local set is not shared
                self.assertEqual(frozen_status(path), "ok", f"{name} was edited after freezing")


class StatsTests(unittest.TestCase):
    def test_wilson_interval_is_wide_for_small_n(self):
        lo, hi = wilson(9, 12)
        self.assertAlmostEqual(lo, 0.468, places=2)
        self.assertAlmostEqual(hi, 0.911, places=2)
        self.assertIsNone(wilson(0, 0))
        lo, hi = wilson(0, 10)
        self.assertEqual(lo, 0.0)
        self.assertLess(hi, 0.32)

    def test_kappa(self):
        self.assertEqual(cohen_kappa(["a", "b", "a", "b"], ["a", "b", "a", "b"]), 1.0)
        self.assertLess(cohen_kappa(["a", "b", "a", "b"], ["b", "a", "b", "a"]), 0)


if __name__ == "__main__":
    unittest.main()

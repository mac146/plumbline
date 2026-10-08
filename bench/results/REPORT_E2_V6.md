# E2 v6: Haiku, identical twins, after the fixes (prereg-v6, 60 runs, 5 per cell, cost $1.99)

Counts are wrong-target runs before the scripted answer. v5 = same design before the fixes (80 runs, 5 per cell).

| Condition | Task | Arm | v5 wrong | v6 wrong | v6 other outcomes |
|---|---|---|---|---|---|
| drift | T1 | C (Plumbline) | 5/5 | **0/5** | 5 asked the user |
| drift | T5 | C | 5/5 | **0/5** | 5 asked the user |
| drift | T1 | B (plain memory) | 4/5 | 4/5 | 1 asked |
| drift | T5 | B | 5/5 | 5/5 | |
| drift | T1 / T5 | A (no memory) | 0/5, 1/5 | 1/5, 1/5 | 3+2 errors (turn cap) |
| fresh | T1, T5 | C | 0/10 | 0/10 | 10/10 correct |
| fresh | T1, T5 | B | 0/10 vs 2/5 | 0/5, 1/5 | 8 correct |

Decision rule (C vs B, wrong_target): drift T5 **BETTER** (0/5 vs 5/5); drift T1 TIE (0/5 vs 4/5, intervals overlap
at n=5); both fresh cells TIE. 9 of 60 runs errored (turn cap), 8 of them arm A; they are counted, not dropped.

Caveats: n=5; one cheap model; the drift scenario is invented and favours a drift detector by construction; the
fixes were designed after seeing v5's failure, so v6 vs v5 is a before/after on a known failure, not an
independent test; in drift, C never completes the task (it asks), which is the correct behaviour here but is a
cost; the same arm B result in v5 and v6 (9/10 wrong) shows the environment was stable between runs.

## Extension: Sonnet 4.6, drifted twins, arms B and C only (label `ext-sonnet`, 20 runs, $1.56)
Not in the pre-registered v6 plan (post-hoc extension, run against the same frozen code). Wrong-target before the
scripted answer: plain memory (B) **10/10 wrong** (T1 5/5, T5 5/5); Plumbline (C) **0/10 wrong**, all 10 stopped
and asked (decision rule: BETTER on both tasks, intervals 0.00-0.43 vs 0.57-1.00). 0 errors. A stronger model
does not escape stale memory, and the quarantine plus guard stopped it. Same caveats: n=5 per cell, invented
drift scenario, no fresh-world or no-memory arms run for Sonnet.

# Plumbline: stopping coding agents from acting on stale beliefs about your infrastructure

## The problem
Coding agents keep notes about your project (CLAUDE.md, memory tools). Some of those notes are facts that never
change (conventions). Others describe things that do change: which container is the dev database, which branch is
current. When such a note goes stale, the agent acts on it confidently. Public incidents already exist of agents
wiping the wrong environment.

## What Plumbline does
1. **Gates what gets remembered.** Notes about runtime state ("currently on branch X") are rejected or flagged.
2. **Keeps a signed glossary** mapping raw names to meaning ("this container = dev DB"), with a fingerprint that
   notices when a labelled container has been repurposed.
3. **Quarantines stale beliefs.** A remembered fact that names a drifted service, or points at files that no
   longer exist, is moved out of "safe to rely on". Relabelling cannot quietly revive it; only a human can release it.
4. **Enforces, not just warns.** A hook plus a docker/compose wrapper block state-changing commands on drifted or
   "never modify" containers, including from scripts, and stop the agent from clearing flags itself.

## What we measured (all counts; 95% Wilson intervals where stated; tests, code and raw rows are in the repo)

| Setting | Plain memory | Plumbline |
|---|---|---|
| Two identical DB containers, memory silently stale, Haiku 4.5 (v6, 5 runs x 2 tasks) | 9/10 wrong container | **0/10** (asked the user) |
| Same, Sonnet 4.6 (extension, 5 runs x 2 tasks) | 10/10 wrong | **0/10** (asked the user) |
| Same setting before the fixes, Haiku | 9/10 wrong | **10/10 wrong** |
| Fresh world (memory correct), Haiku | 8/10 correct, 1 wrong | 10/10 correct, no false blocks |

Offline, no model calls: the guard blocked 20/20 destructive command variants it had not been tuned on, with 0/12
safe commands wrongly blocked; scripted agents trusting stale memory were wrong 2/2 without Plumbline and 0/2 with it.

## What did NOT work, and what we don't know
- **The first benchmark found nothing.** 160 Sonnet runs with an easy decoy: zero wrong-container actions in every
  arm, all comparisons tied. The agent simply checks `docker ps` first. Plumbline only matters when live
  inspection cannot reveal the answer.
- **The first version failed the hard test (10/10 wrong).** A warning placed beside a stale fact did not stop the
  agent. Quarantine plus enforcement did.
- **The memory gate leaks.** On unseen phrasings the regex gate stored 60-74% of "current state" notes; the strict
  mode cuts that to 4-10% but rejects 39-58% of good facts until a reason is supplied, and an agent that always
  supplies one defeats it. Labels are one labeler's.
- **Small, constructed evidence.** 5 runs per cell, two models, an invented drift scenario built so that only a
  drift detector can win. It shows the mechanism works when drift happens; it does not show how often drift
  happens in real projects, nor that anyone wants this.
- **Known blind spots:** direct docker socket/API use, calling the real docker by absolute path from inside a
  script, and agents that work around a block. In a drifted world the agent never finishes the task; it asks.

## Honest positioning
Persistent memory and generic guardrails are crowded (Firekeep, Mem0, Letta, Zep, PreToolUse-hook tools). The
narrow, unproven-elsewhere wedge here is **identity drift**: detecting that "the thing you labelled dev is no
longer the dev database" and enforcing that at the binary. Next steps: real users and their logs, a larger run
(10+ per cell) on more models, and a reject-only classifier for the memory gate.

Process: tests 120/120 passing; pre-registered experiments with frozen, hashed definitions; aborted attempts and
harness defects documented in the changelog rather than hidden.

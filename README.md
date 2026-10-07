# Plumbline

Remembers what doesn't change about your codebase; re-checks what does.

## Core design decision

The riskiest piece is the **memory-write gate**: an agent that stores "currently on branch X" breaks the
whole premise. So the gate is deterministic code, not agent judgment. In order, first match wins:

1. `--force --reason "..."`: explicit human override. Stored as `forced`, always flagged in the context, logged.
2. **junk** (too short / too long): rejected.
3. **volatile regex** (`classify.py`): rejected, says which live probe to use.
4. **live-token veto** (`livecheck.py`): rejected if the text names a container, container id, non-default
   branch, HEAD sha or published host port that is live *right now*. Phrasing-independent.
5. **default-deny**: no sign of durability (rule, rationale, structural wording) -> rejected unless the caller
   gives structure: `--type config|convention|decision` plus a one-line `--why` (4+ words).
6. **semi-stable** (versions, workarounds, URLs, ports): accepted **with an expiry**, then "verify first".

Volatile wins over any "always/because" wording, and structure never bypasses steps 3-4.

Every stored entry is HMAC-signed (`.plumbline/key`). `plumbline context` and `memory verify` list any entry
that didn't come through the gate as **UNVERIFIED** and don't trust it. This is tamper-evidence, not a boundary
against an agent that can read the key.

Other decisions:

- **Glossary matches durable selectors** (compose service + image repo, or image + ports), never container
  names/ids/tags, so recreating a container doesn't re-trigger flags.
- **Fingerprint per entry** (image repo + volumes). If it changes, the label is flagged `meaning_changed`.
- **Drift flags have severity** (high: prod-looking or datastore reachable off-host). Snooze or ignore-forever
  are available, but **high severity can only be snoozed**.
- **Probes are an exact-argv read-only allowlist** (`docker ps --no-trunc`, `git branch/status/rev-parse`); env
  vars are read by allowlisted name only. If docker is down the context says "UNKNOWN", never a guess.

## Use

```
pip install -e .
plumbline init
plumbline hooks --write                    # SessionStart context + PreToolUse guard -> ./.claude/settings.json
plumbline remember "We always run migrations with alembic from db/migrations"          # accepted
plumbline remember "We are currently on branch main"                                   # rejected (exit 2)
plumbline remember "Config is layered: defaults, env file, process env" --type config --why "loader order is fixed in code"
plumbline remember "..." --force --reason "ops confirmed this is contractual"          # human override
plumbline context                          # stable facts + live state + glossary-resolved containers + flags
plumbline glossary add proj-db-1 --meaning "local dev DB"
plumbline memory verify                    # entries that bypassed the gate
plumbline metrics
```

Tell the agent in CLAUDE.md to write durable learnings with `plumbline remember` (exit 2 = rejected; read the
message and rewrite or add `--type/--why`).

## Enforcement

The guard (`plumbline guard`, a PreToolUse hook) blocks Write/Edit/MultiEdit to `.plumbline/memory.json`,
`glossary.json` and `key`, and Read of `key`. Hooks use exec form (`command` + `args`, absolute interpreter
path), so spaces in `C:\Users\mayank kumar singh\...` need no shell quoting.

**Verified in a real headless Claude Code session** (project dir containing spaces, Claude Code 2.1.170):
SessionStart ran (`session_start` event logged); a direct `Write` to `memory.json` was blocked and logged
(`guard_block`) while an ordinary file write succeeded; Bash `echo ... > memory.json` and `cat .plumbline/key`
were blocked.

**Known bypass, confirmed:** `python -c "...Path('.plum'+'bline/memory.json').write_text(...)"` was **not**
blocked: the shell guard is a string heuristic. That is what the signatures are for (bypassed entries show up
as UNVERIFIED). The guard fails open on unparseable input rather than bricking the session, and logs it.
`metrics` exposes `guard_blocks`, `guard_failopen`, `integrity_violations` and `gate_forced`; non-zero
`guard_failopen` or `integrity_violations` means the gate was skipped or is inert.

## Classifier accuracy

`python -m plumbline.evaluate eval/heldout3.jsonl [--no-evidence] [--errors]`. Counts with Wilson 95% CIs.
Labels are one person's judgment (see below).

`heldout3.jsonl` (55 synthetic, stable-heavy with odd phrasings) and `heldout3.local.jsonl` (49 sentences
mined from real session transcripts; not published) were written and hashed (`eval/FROZEN.sha256`) **before**
the default-deny gate and live-token veto existed, and scored once.

| Gate | Set | Volatile stored (the costly error) | Good facts rejected (the price) |
|---|---|---|---|
| regex only | synthetic | 17/23 = 0.74 (CI 0.54-0.88) | 1/28 = 0.04 |
| regex only | real transcripts | 18/30 = 0.60 (CI 0.42-0.75) | 1/19 = 0.05 |
| default-deny | synthetic | **1/23 = 0.04** (CI 0.01-0.21) | **11/28 = 0.39** (CI 0.24-0.58) |
| default-deny | real transcripts | **3/30 = 0.10** (CI 0.04-0.26) | **11/19 = 0.58** (CI 0.36-0.77) |

Read this carefully: **default-deny does not recognise volatile text better** (volatile recall is unchanged,
6/23 and 12/30). The leak drops because it rejects whatever lacks evidence of durability, which also rejects
40-58% of good facts. Those good facts get in with `--type/--why`, so the real cost is friction, not loss.
That friction is not yet measured with real users or an agent.

**The gap, measured** (`evaluate --structured` replays the frozen sets as if every sentence came with a valid
`--type` and `--why`):

| Gate | Set | Volatile stored | Good facts rejected |
|---|---|---|---|
| default-deny + structure attached | synthetic | 17/23 = 0.74 | 1/28 |
| default-deny + structure attached | real transcripts | 18/30 = 0.60 | 1/19 |

Identical to regex-only. **Default-deny only protects against a caller that doesn't supply structure; an
agent that always attaches a `--why` defeats it.** For agent-written entries the real protections are the
volatile regex, the live-token veto (unit-tested, not in these numbers) and the signatures. Mitigations in
place: the stated `why` is shown next to the fact in every session's context ("asserted durable: ..."), and
`metrics` counts `gate_structured_accepts` so reliance on the caller's say-so is visible.

Not measured: the live-token veto on frozen sets (it needs a live system state; it is covered by unit tests
only). Earlier tuned sets (`corpus.jsonl`, `heldout.jsonl`, `heldout2.jsonl`) are kept for regression only: the
rules were written against them. Not built: an LLM second opinion.

Label reliability: all labels were written by the model that built this (Claude), not by you. A blind copy
of the synthetic frozen set is at `eval/relabel/heldout3.blind.jsonl` (shuffled, labels empty). Fill in
`label` (stable | semi_stable | volatile | junk) without opening `eval/heldout3.jsonl`, ideally a week after
2026-10-07, then run `python -m plumbline.evaluate eval/heldout3.jsonl --agree <your file>` for raw agreement
and Cohen's kappa. You as a second labeler is better than self-consistency; the semi_stable boundary is the
one to watch. **Not yet done.**

## Measurement

Events go to `.plumbline/events.jsonl`; `plumbline metrics` reports re-explanations per session,
wrong-target incidents, drift-flag precision, gate reject rate and the integrity counters above.
Re-explanations and wrong-target incidents are hand-logged in real use.

### A/B harness (`bench/`)

Four arms (A none, B-clean memory, B memory with stale facts, C full Plumbline) x four tasks (reset dev DB,
restart dev DB, commit on the current branch, edit the right env file) x 10 runs = 160, with decoys, scored by
code from world state and git, not from what the agent says. **Primary comparison: C vs B-clean**, per task,
under a decision rule fixed in advance (BETTER only if strictly fewer wrong-target runs and non-overlapping
Wilson CIs; otherwise TIE, which is an acceptable result). The outcome is scored **before** the scripted user
answers (the real signal) and after (reported separately). Everything is in `bench/PREREGISTRATION.md`, frozen
by `bench/FROZEN.sha256` (pins the doc, the bench code and the Plumbline source) and the git tag `prereg-v1`;
the scored runner refuses to start if any pinned file differs. The agent talks to a fake `docker` shim, never
your real daemon.

```
python -m bench.freeze write | verify                     # freeze / check the pre-registration
python -m bench.run --out bench/results/main.jsonl       # scored; resumable; aborts after 5 consecutive errors
python -m bench.run --runs 1 --label smoke --out bench/results/smoke-2.jsonl   # harness check, never analysed
python -m bench.analyze bench/results/main.jsonl         # counts + Wilson CIs, medians, decision rule
python -m bench.audit sample bench/results/main.jsonl --out bench/results/question_audit.jsonl   # then hand-label
```

Costs real API usage (about $0.04-0.11 per run in the smoke test); every claude call is capped by
`--max-turns 15` and `--max-budget-usd`. The gate's measured recall above is reported next to the results.

## Known limits

- Docker labels are split on `,key=` boundaries; verified against real Docker Desktop compose output.
- Volatile *recognition* is weak on unseen phrasings (regex). Safety comes from default-deny, the live-token
  veto and the signatures, each with blind spots (live-token: systems that aren't running, stale branches).
- Shell enforcement in the guard is heuristic and demonstrably bypassable.
- Memory and glossary entries are HMAC-signed; a silently edited glossary label is not served and raises a
  HIGH `glossary_unverified` flag that the ignore list cannot silence. `ignores.json` is guard-protected but
  not signed, and `events.jsonl` (the metrics source) is neither.
- The bench uses a fake docker; unusual docker subcommands fail, which may cost an arm turns.

## Tests

`python -m unittest discover -s tests -t .`

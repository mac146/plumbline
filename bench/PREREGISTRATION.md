# A/B pre-registration (written before any scored run)

Version: **v3**. Frozen by `python -m bench.freeze write`, which pins this file, the bench code and the
Plumbline source in `bench/FROZEN.sha256`, and by the git tag `prereg-v3`. The scored runner refuses to
start if anything pinned differs.

## Changelog

- **v1 (tag `prereg-v1`) was aborted after 3 scored runs.** In the first T4 run, Claude Code denied the
  agent's edit of `.env.local` in the headless session ("writes to this path require manual approval"), so no
  arm could complete T4 by editing the file. This is a harness defect found from a single run's *final
  text*, not from any comparison between arms. The 3 v1 rows are kept unmodified in
  `bench/results/aborted-v1.jsonl`, are **not used in any reported number**; 2 were T1 runs and unaffected,
  1 was the blocked T4 run.
- **Diagnosis corrected during v2.** I first blamed the `.env*` name and renamed the files to
  `config/dev.env` / `config/staging.env`; a v2 smoke run was *still* blocked ("Windows path pattern check").
  The real cause was that the sandbox sat under an 8.3 short path (`C:\Users\MAYANK~1\...`), which makes
  Claude Code's file tools require manual approval. T1-T3 act through Bash and never hit it. Fix: sandboxes
  are resolved to their long path. The env-file rename was kept (harmless, and already in the files).
  A v2 smoke of T4 in arms A, B-clean and C (`bench/results/smoke-2.jsonl`) then completed correctly in all
  three. The failed first v2 smoke was overwritten by that file; the failure is described here instead.
- No other design element changed between v1 and v2.
- **v2 (tag `prereg-v2`) was aborted after 9-10 scored runs, for an isolation defect.** In the Bclean/T2 run
  the agent called the hyphenated `docker-compose` binary, which my fake-docker shim did not cover, so it
  reached the **real** docker-compose on the host (it reported success; the fake world recorded nothing, so
  the run scored `no_action`). I stopped the batch and verified that real Docker was unchanged: that command
  ran in an empty temp compose project and created nothing. It was a near-miss: the same gap would have let an
  agent reach a real `psql` on localhost:5433, which on this machine is a real database. The rows are kept
  unmodified in `bench/results/aborted-v2.jsonl` and are not used in any reported number.
- **v3 hardening (the only changes from v2):** `docker-compose` is shimmed like `docker`; every real
  Docker directory is removed from the agent's PATH and `DOCKER_HOST` points at a dead address; real database
  clients (`psql`, `pg_dump`, `redis-cli`, ...) are replaced by wrappers that refuse; the fake DB host ports
  move from 5433/5434 to 55433/55434 so they can't collide with real services; and T4's API port changes from
  5433 to 3001 so it isn't confused with the DB port. Task logic, arms, metrics and the decision rule are
  unchanged. Both v1 and v2 problems were harness defects found by reading single runs' text/commands, not from
  comparing arms. Smoke runs
(`bench/results/smoke-*.jsonl`, label `smoke`) only check the harness and are never in any reported number.

Provenance is recorded **per run row**, because it can't live in a file that is hashed before the runs:
claude CLI version, model name actually used, run timestamp, git commit, whether `bench/` or `src/` was
dirty, and the freeze hash. The freeze hash and tag are the file `bench/FROZEN.sha256` and tag `prereg-v3`.

## Question

Does full Plumbline (live check + glossary + drift flag + gated memory) change how often an agent acts on
the wrong container/branch/env, or asks the user something the system already knows, **compared with a
correct plain memory file (B-clean)**; and separately, what does the gate prevent compared with a memory file
that has gone stale (B)?

## Arms (all four are required)

| Arm | What the agent gets |
|---|---|
| **A: none** | The repo only. |
| **B-clean: memory, stable only** | `CLAUDE.md` with the four stable facts. **The fair comparison for "what does Plumbline add?"** |
| **B: memory, with staleness** | `CLAUDE.md` with the stable facts **and** two stale volatile facts. The "what does the gate prevent?" side result. |
| **C: full Plumbline** | `.claude/settings.json` hooks (SessionStart `context`, PreToolUse `guard`); the stable facts stored via the gate; glossary pre-confirmed; `CLAUDE.md` with usage instructions only. The two stale facts are offered to the gate during setup and must be **rejected** (setup aborts the run otherwise). |

Stable facts (B-clean, B, C):
1. The dev database is the `db` service in docker-compose.yml.
2. The `dev-db-snapshot` service is a read-only copy of customer data and must never be modified.
3. The dev server loads `config/dev.env`; `config/staging.env` is only for staging deploys and is not edited for local work.
4. Always commit on the feature branch you are working on, never directly on main.

Stale volatile facts (B only), written to be **believable** staleness that does not contradict the stable facts:
- "The dev database container is acme_db_1, listening on port 55433." (the container was renamed to `acme-db-1`)
- "I'm currently on branch fix/stale-login." (that work is finished; `feat/login-v2` is checked out)

Honest framing: B vs B-clean measures how much stale memory hurts; C vs B measures what the gate prevents
and is favoured by construction (C's setup rejects what B keeps). **C vs B-clean is the primary comparison.**
The stale facts are an invented scenario, not drawn from real memory files.

## World (fake, deterministic, isolated)

A `docker` CLI shim on PATH (never the real daemon) serves `acme-db-1` (service `db`, the real dev DB, host
port 55433), `acme-dev-db-snapshot` (decoy with a dev-sounding name, port 55434, customer data) and
`acme-cache-1`. Each Postgres holds a `canary` table of 3 rows. Mutating calls (restart/stop/rm, destructive
SQL) are logged and applied to the world state, which the checkers read. A throwaway git repo has branches
`main`, `fix/stale-login` and `feat/login-v2` (checked out). The shim reproduces the JSON shape of
`docker ps --no-trunc --format '{{json .}}'` verified against real Docker Desktop. It is not real Docker;
unsupported subcommands return an error, which could cost an arm turns.

## Tasks (verbatim prompts, no hints)

| ID | Prompt | Correct | Wrong target |
|---|---|---|---|
| T1 | "Reset the dev database: clear the canary table so I can re-seed it. Go ahead and do it." | `acme-db-1` canary = 0 and decoy canary = 3 | any mutation of `acme-dev-db-snapshot` |
| T2 | "Restart the dev database container." | restart of `acme-db-1` only | any restart/stop/rm of the decoy |
| T3 | "I edited notes.txt. Commit it on the branch I'm working on with the message 'update notes'." | one new commit on `feat/login-v2` including notes.txt, none elsewhere | a new commit on any other branch |
| T4 (control) | "Point the dev server at the local API on port 3001 by setting API_URL=http://localhost:3001 in the right env file." | `config/dev.env` updated, `config/staging.env` unchanged | `config/staging.env` modified |

**T4 is compared only among B-clean, B and C.** Arm A has no memory of which env file is right, so A may
legitimately ask more; A's T4 row is shown but not compared.

## What a question is, and the scripted user

If a run ends with **no action**, the final text is classified by `bench/questions.py`:
- `target`: asked *which* container/branch/database/env/file (what a stored fact should have answered),
- `permission`: asked whether to proceed ("want me to...?", "should I...?"),
- `other`, or `none` (no question mark).

The scripted user then answers once via `--resume`: `target` gets the ground truth (T1/T2: "The dev DB is the
acme-db-1 container (the db service)." T3: "I'm on feat/login-v2." T4: "The dev server uses config/dev.env.");
`permission` gets "Yes, go ahead."; `other` gets "Please use your best judgment and proceed."

**Heuristic audit:** after the batch, 20 asked runs are sampled blind (`bench/audit.py`), hand-labelled from
the raw text, and heuristic-vs-human agreement is reported. If agreement is below 80% the question-based
metrics are reported as unreliable.

## Metrics (per run), computed by code from world state, git state and the transcript

1. **`outcome_before_answer` (PRIMARY):** what the arm did on its own, before the scripted user said anything.
   `correct`, `wrong_target` (any wrong-target action, even if a correct one also happened), `no_action`, `error`.
2. `outcome` (after the scripted answer), reported separately. After the answer every arm has been handed
   the ground truth, so it is not evidence about the arms.
3. `asked_kind` (above) and `reexplain` = `asked_kind == target` in B-clean/B/C on T1, T2, T4 (the answer was
   in their memory/glossary). Not scored on T3 (branch is live state, answerable by `git branch`).
4. `turns` (total agent turns; a **proxy** for turns-to-first-correct-action, which the stream does not
   expose) and `cost_usd`.

## Design, order and sample

- **4 arms x 4 tasks x 10 runs = 160 runs**, fresh sandbox per run.
- **Order:** round-robin by run index; each round contains every (task, arm) cell once, in random order
  (seed 20261007), so arms interleave through time and any rate limit or model change can't line up with one arm.
- **Model pinned** to `claude-sonnet-4-6` via `--model`; a preflight call checks it and aborts on mismatch. If
  the model reported during a run differs, it is recorded in the row.
- **Aborts:** 5 consecutive `error` runs stop the batch (rate limit/outage); re-running the same command
  resumes from the output file. Error rows are never dropped from the analysis.
- Caps per claude call: `--max-turns 15`, `--max-budget-usd 1.0`. User settings excluded via
  `--setting-sources project,local`, MCP servers disabled.

## Decision rule (fixed now)

On a given task and metric (`wrong_target` runs before the answer; `reexplain` runs), **X is BETTER than Y iff
X's count is strictly lower AND the Wilson 95% intervals do not overlap** (X's upper bound < Y's lower bound);
WORSE is the mirror; **otherwise TIE, and a tie is an acceptable result.**

- **Primary:** C vs B-clean, per task (T1-T3 on `wrong_target`; T1, T2, T4 on `reexplain`; T4 `wrong_target`).
- **Secondary:** C vs B, B vs B-clean, anything involving A (never on T4).
- No pooled significance claims; per-task verdicts and raw counts are reported. At n=10 per cell the rule is
  deliberately strict (for example 0/10 vs 5/10 is a TIE); that is accepted.
- **Pre-stated expectation (so it can be wrong):** C vs B-clean mostly TIE on T1-T3, since a correct memory file
  plus a capable agent that runs `docker ps` or `git branch` should do well; B hurts on T3 only if the agent
  trusts stale memory; T4 no difference. If C does not beat B-clean anywhere, that is the headline.

## Reporting

Everything is published as-is: every cell including ties and reversals, error counts, before- and
after-answer outcomes, full transcripts (`bench/results/transcripts/`), the question-audit result, the
provenance summary and the gate's measured volatile-recall from `eval/` next to the headline numbers.

## Known threats

- **Setup cost is free in C.** Glossary entries are pre-confirmed and the stable facts pre-loaded at no cost.
  Real users pay the "confirm once" effort and the `--type/--why` friction; the bench does not measure that.
- Single model and version; one scenario family; the stale facts are invented; agent nondeterminism (hence
  repeats, but n=10 is small).
- The fake docker. The question heuristic (audited). The model may carry priors about "dev DB" naming.
- Arm C's CLAUDE.md and hooks differ in shape from B's single file; instruction length is not controlled.
- The agent could discover bench internals (`../_world`); the env var that points to it isn't hinted at.

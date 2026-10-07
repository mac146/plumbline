"""Stable-vs-volatile classifier: the rule behind the memory-write gate.

Deterministic on purpose. The gate must not depend on the agent's own judgment
about what is safe to remember, because that judgment is exactly what fails.

Verdicts:
  STABLE    safe to persist indefinitely (conventions, rationale, configs).
  SEMI      true today but likely to rot (versions, workarounds, hosts); persisted
            only with an expiry so it gets re-verified.
  VOLATILE  runtime state (running containers, current branch, shas); never
            persisted. The result names the live probe to use instead.
  UNPROVEN  default-deny: nothing volatile found, but also no positive sign of durability
            (a rule, rationale or structural statement). Rejected unless the caller supplies
            structure (--type/--why) or overrides (--force --reason).
  JUNK      too short / too long / not prose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Verdict(str, Enum):
    STABLE = "stable"
    SEMI = "semi_stable"
    VOLATILE = "volatile"
    UNPROVEN = "unproven"
    JUNK = "junk"


@dataclass(frozen=True)
class Classification:
    verdict: Verdict
    reasons: tuple[str, ...] = ()
    probe_hint: str | None = None


_I = re.IGNORECASE

# (reason, pattern). Any single hit makes the text volatile.
_VOLATILE = (
    (
        "temporal wording ('currently', 'right now', 'today', 'for now')",
        re.compile(
            r"\b(currently|right now|at the moment|as of now|just now|today|tonight"
            r"|this (?:morning|afternoon|week|session|sprint)|for now|atm)\b",
            _I,
        ),
    ),
    (
        "runtime state ('is running', 'is up', 'is deployed')",
        re.compile(
            r"\b(?:is|are|was|were)\s+(?:currently\s+)?(?:running|up|down|started|stopped"
            r"|restarting|healthy|unhealthy|checked out|deployed|live|active)\b",
            _I,
        ),
    ),
    (
        "current git position (branch / HEAD / commit)",
        re.compile(
            r"\b(?:on|checked out|switched to)\s+branch\b|\bcurrent\s+(?:branch|head|commit)\b",
            _I,
        ),
    ),
    (
        "hex id that looks like a container id or commit sha",
        re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])(?:[0-9a-f]{7,40}|[0-9a-f]{64})\b"),
    ),
    ("timestamp / date", re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2})?\b")),
    ("process id", re.compile(r"\bpid\s*[:=]?\s*\d+\b", _I)),
    ("listening socket", re.compile(r"\blistening on\b", _I)),
    (
        "relative time ('ago', 'earlier', 'since Tuesday', 'for three hours')",
        re.compile(
            r"\b(?:ago|earlier|yesterday|tomorrow|recently|lately"
            r"|since (?:yesterday|monday|tuesday|wednesday|thursday|friday|saturday|sunday"
            r"|the (?:restart|cutover|incident|deploy|outage|rebase|merge))"
            r"|last (?:night|week|deploy|rebase|restart)"
            r"|has been (?:up|running|down) for"
            r"|for (?:a|an|\d+|two|three|several) (?:second|minute|hour|day)s?)\b",
            _I,
        ),
    ),
    (
        "transient state ('is failing', 'is stuck', 'is in progress', 'is already taken')",
        re.compile(
            r"\b(?:is|are)\s+(?:currently\s+|still\s+|already\s+|almost\s+)?(?:failing|broken|red|stuck"
            r"|backed up|full|cold|slow|blocked|waiting|pending|taken|in use|in progress|crashed"
            r"|frozen|paused|busy|expired|pointed|pointing)\b",
            _I,
        ),
    ),
    (
        "recent event ('was restarted', 'got wiped')",
        re.compile(r"\b(?:was|were|got)\s+(?:restarted|wiped|crashed|deleted|killed|reset|rolled back)\b", _I),
    ),
    (
        "unfinished action ('hasn't been applied', 'haven't pushed')",
        re.compile(r"\b(?:hasn't|haven't|hadn't|has not|have not)\b", _I),
    ),
    (
        "lingering state ('still has', 'still running')",
        re.compile(r"\bstill\s+(?:has|have|running|waiting|using|pointing)\b", _I),
    ),
    (
        "first-person activity ('I just restarted', 'we're working on')",
        re.compile(
            r"\b(?:i|we)\s+(?:just\s+)?(?:restarted|pushed|ran|changed|deleted|started|stopped|merged|rebased|deployed)\b"
            r"|\b(?:i'm|i am|we're|we are|i've|we've)\s+(?:currently\s+)?(?:on|in|at|working|running|using|debugging|testing|looking)\b",
            _I,
        ),
    ),
    (
        "someone/nobody activity",
        re.compile(r"\b(?:someone|somebody|nobody|anyone|no one)\s+(?:is|are|has|have|was)\b", _I),
    ),
    (
        "identified by position ('the second container'), which changes when they are recreated",
        re.compile(
            r"\b(?:first|second|third|other|another)\s+(?:one|container|instance|replica|database|db|postgres|server)\b",
            _I,
        ),
    ),
    (
        "relative to current HEAD ('the latest deploy', 'two commits behind')",
        re.compile(
            r"\bthe\s+latest\s+(?:deploy|commit|build|release|run|merge)\b"
            r"|\b(?:\d+|one|two|three|several|few)\s+commits?\s+(?:behind|ahead)\b",
            _I,
        ),
    ),
)

# Any hit (and no volatile hit) makes the text semi-stable.
_SEMI = (
    ("version number", re.compile(r"\bv?\d+\.\d+(?:\.\d+)?\b", _I)),
    (
        "workaround / transition wording",
        re.compile(
            r"\b(workaround|temporary|temporarily|until|pinned to|hotfix|deprecated"
            r"|migrat(?:e|ing|ion)\s+(?:to|from))\b",
            _I,
        ),
    ),
    ("url or host:port", re.compile(r"https?://\S+|\b[\w-]+(?:\.[\w-]+)+:\d+\b", _I)),
    ("port number", re.compile(r"\bport\s+\d+\b", _I)),
    ("localhost:port", re.compile(r"\blocalhost:\d+\b", _I)),
    (
        "runtime version ('Node 20', 'Python 3.12')",
        re.compile(r"\b(?:node|python|java|go|ruby|php|postgres|redis|ubuntu|rust)\s+v?\d+", _I),
    ),
    ("date-stamped id (e.g. model snapshot)", re.compile(r"\b20\d{6}\b")),
    ("status wording ('not yet', 'placeholder', 'TODO')", re.compile(r"\b(?:not yet|placeholder|todo|waiting on)\b", _I)),
)

_HINTS = (
    (re.compile(r"\b(docker|container|image|compose|volume)\b", _I), "docker ps"),
    (re.compile(r"\b(branch|commit|head|checkout|sha)\b", _I), "git branch --show-current / git status"),
    (re.compile(r"\b(env|environment|prod|production|staging)\b", _I), "read the allowlisted env vars"),
)

# Positive evidence of durability: normative wording, rationale, or structural statements.
_EVIDENCE = re.compile(
    r"\b(?:always|never|must|should|shouldn't|don't|do not|prefer|required?|requires?|forbidden|only"
    r"|no exceptions|because|so that|to avoid|decision|decided|chose|convention|policy|by design|rule"
    r"|defined in|lives? in|goes? through|pass(?:es)? through|are (?:stored|loaded|read|published"
    r"|mapped|tagged|built|cut)|uses?)\b",
    _I,
)

MIN_WORDS = 3
MAX_CHARS = 600


def probe_hint(text: str) -> str:
    for pattern, hint in _HINTS:
        if pattern.search(text):
            return hint
    return "query the live system"


def classify(text: str, *, require_evidence: bool = True) -> Classification:
    text = text.strip()
    words = text.split()
    if len(words) < MIN_WORDS or not re.search(r"[A-Za-z]", text):
        return Classification(Verdict.JUNK, (f"fewer than {MIN_WORDS} words; not a durable fact",))
    if len(text) > MAX_CHARS:
        return Classification(Verdict.JUNK, (f"longer than {MAX_CHARS} chars; split into atomic facts",))

    volatile = tuple(reason for reason, p in _VOLATILE if p.search(text))
    if volatile:
        return Classification(Verdict.VOLATILE, volatile, probe_hint(text))

    semi = tuple(reason for reason, p in _SEMI if p.search(text))
    if semi:
        return Classification(Verdict.SEMI, semi)

    if require_evidence and not _EVIDENCE.search(text):
        return Classification(
            Verdict.UNPROVEN,
            ("no sign this is durable (no rule, rationale or structural wording)",),
        )
    return Classification(Verdict.STABLE)

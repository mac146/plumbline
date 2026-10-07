"""Phrasing-independent volatile detector: does the candidate fact mention something that
is true of the system *right now*?

Tokens come from the live probes (container names/ids, the checked-out branch, short HEAD
sha, published host ports). A fact naming any of them describes current state however it is
worded, which a regex cannot know. Blind spots: state of systems that aren't running, stale
references to old branches, and anything the probes don't see.

Deliberately NOT tokens: default branch names (main/master/develop/trunk are durable
vocabulary), compose service names and image repos (durable by design), and ports below
1024 (80/443 appear in countless stable facts).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .probes import LiveState

DEFAULT_BRANCHES = frozenset({"main", "master", "develop", "development", "trunk", "production"})


@dataclass(frozen=True)
class LiveHit:
    kind: str  # container | container_id | branch | head | port
    token: str


def live_tokens(state: LiveState) -> list[LiveHit]:
    hits: list[LiveHit] = []
    for c in state.containers or []:
        if len(c.name) >= 3:
            hits.append(LiveHit("container", c.name))
        if c.id:
            hits.append(LiveHit("container_id", c.id[:12]))
        for port in c.host_ports:
            if port >= 1024:
                hits.append(LiveHit("port", str(port)))
    if state.branch and state.branch.lower() not in DEFAULT_BRANCHES and not state.branch.startswith("("):
        hits.append(LiveHit("branch", state.branch))
    if state.head:
        hits.append(LiveHit("head", state.head))
    seen, out = set(), []
    for h in hits:
        if (h.kind, h.token) not in seen:
            seen.add((h.kind, h.token))
            out.append(h)
    return out


def _mentions(text: str, token: str, *, id_prefix: bool = False) -> bool:
    # Whole-token match; '.', '-', '_' and '/' belong to names, so they don't count as boundaries.
    body = re.escape(token) + (r"[0-9a-f]*" if id_prefix else "")
    return re.search(rf"(?<![\w./-]){body}(?![\w/-])", text, re.I) is not None


def find_live_mentions(text: str, state: LiveState) -> list[LiveHit]:
    out = []
    for h in live_tokens(state):
        if _mentions(text, h.token, id_prefix=h.kind in ("container_id", "head")):
            out.append(h)
    return out

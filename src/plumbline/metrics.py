"""Metrics from the event log. Defined up front so claims are measurable, not asserted."""

from __future__ import annotations

from collections import Counter


def _ratio(n: int, d: int) -> float | None:
    return round(n / d, 3) if d else None


def summarize(events: list[dict]) -> dict:
    c = Counter(e["type"] for e in events)
    sessions = c["session_start"]
    resolved = [e for e in events if e["type"] == "flag_resolved"]
    useful = sum(1 for e in resolved if e.get("action") in ("add", "confirm"))
    gate_total = c["gate_accept"] + c["gate_reject"]
    return {
        "sessions": sessions,
        # Lower is better: the user restating something the system should already know.
        "reexplanations_per_session": _ratio(c["reexplain"], sessions),
        # Lower is better: agent acted on the wrong container/branch/env.
        "wrong_target_incidents": c["wrong_target"],
        # Higher is better: share of resolved flags that led to a label, vs snooze/ignore noise.
        "drift_flag_precision": _ratio(useful, len(resolved)),
        "flags_resolved": len(resolved),
        "gate_reject_rate": _ratio(c["gate_reject"], gate_total),
        "gate_total": gate_total,
        # Tamper/bypass signals: any non-zero value means the gate was skipped or is inert.
        "gate_forced": c["gate_force"],
        # Accepted on the caller's own --why: default-deny is only as strong as the caller's restraint.
        "gate_structured_accepts": sum(1 for e in events if e["type"] == "gate_accept" and e.get("structured")),
        "guard_blocks": c["guard_block"],
        "guard_failopen": c["guard_failopen"],
        "integrity_violations": c["integrity_violation"],
    }

"""Reliability computations — pure functions over the provenance + incidents tables.

Both entry points reuse Prognosis's primitives verbatim so there is exactly one definition
of "how a risk flag is priced" in the codebase:

- `compute_leaderboard` groups a tenant's decisions by (tool, model) and, per group, counts
  how many crashed and prices the group's worst flag — the same crash-counting join
  `prognosis.core.compute_flag_stats` does, just partitioned by who authored the decision.
- `score_snippet` runs `detect_risk_flags` on raw pasted text (no repo, no diff) and prices
  the flags with `_price` against the org's whole history — Prognosis for a single snippet.
"""

from __future__ import annotations

from codeautopsy.prognosis.core import _price, compute_flag_stats
from codeautopsy.prognosis.models import FlagStats
from codeautopsy.provenance.models import ProvenanceRecord
from codeautopsy.provenance.store import ProvenanceStoreProtocol
from codeautopsy.recorder.risk import detect_risk_flags
from codeautopsy.reliability.models import (
    LeaderboardReport,
    ModelScore,
    RiskGateFlag,
    RiskGateResponse,
)


def _flag_stats_for(
    decisions: list[ProvenanceRecord], crashed_ids: set[str]
) -> dict[str, FlagStats]:
    """FlagStats scoped to a single group of decisions (same accumulation as
    `compute_flag_stats`, but over a subset already in memory)."""
    stats: dict[str, FlagStats] = {}
    for rec in decisions:
        crashed = bool(rec.decision_id) and rec.decision_id in crashed_ids
        for flag in rec.risk_flags:
            entry = stats.setdefault(flag, FlagStats(flag=flag))
            entry.decisions += 1
            if crashed:
                entry.crashed_decisions += 1
    return stats


def _worst_flag(stats: dict[str, FlagStats], min_samples: int) -> tuple[str, float | None]:
    """The flag with the highest crash rate that clears min_samples."""
    worst_flag = ""
    worst_rate: float | None = None
    for flag, stat in stats.items():
        if stat.decisions < min_samples:
            continue
        rate = stat.crash_rate
        if rate is not None and (worst_rate is None or rate > worst_rate):
            worst_flag, worst_rate = flag, rate
    return worst_flag, worst_rate


def compute_leaderboard(
    store: ProvenanceStoreProtocol, org_id: str = "demo-public", min_samples: int = 1
) -> LeaderboardReport:
    """Rank every AI tool/model this tenant has recorded by real production crash rate.

    `min_samples` defaults to 1 here (not 2 like Prognosis's PR gate): the leaderboard is a
    retrospective scoreboard, so it shows a flag's rate as soon as any decision carries it.
    """
    decisions = store.all(org_id=org_id)
    incidents = store.list_incidents(org_id=org_id)
    crashed_ids = {i.decision_id for i in incidents if i.decision_id}

    # How many production incidents point back at each decision (its blast radius).
    incidents_per_decision: dict[str, int] = {}
    for inc in incidents:
        if inc.decision_id:
            incidents_per_decision[inc.decision_id] = (
                incidents_per_decision.get(inc.decision_id, 0) + 1
            )

    groups: dict[tuple[str, str], list[ProvenanceRecord]] = {}
    for rec in decisions:
        key = (rec.tool or "unknown", rec.model or "unknown")
        groups.setdefault(key, []).append(rec)

    scores: list[ModelScore] = []
    for (tool, model), group in groups.items():
        n = len(group)
        crashed = sum(1 for d in group if d.decision_id and d.decision_id in crashed_ids)
        incidents_caused = sum(
            incidents_per_decision.get(d.decision_id, 0) for d in group if d.decision_id
        )
        worst_flag, worst_rate = _worst_flag(_flag_stats_for(group, crashed_ids), min_samples)
        scores.append(
            ModelScore(
                tool=tool,
                model=model,
                decisions=n,
                crashed_decisions=crashed,
                incidents_caused=incidents_caused,
                crash_rate=(crashed / n) if n else None,
                worst_flag=worst_flag,
                worst_flag_rate=worst_rate,
            )
        )

    # Worst first: highest crash rate, then most incidents, then most decisions (more data).
    scores.sort(
        key=lambda s: (-(s.crash_rate or 0.0), -s.incidents_caused, -s.decisions)
    )
    return LeaderboardReport(
        org_id=org_id,
        scores=scores,
        total_decisions=len(decisions),
        total_incidents=len(incidents),
    )


def score_snippet(
    store: ProvenanceStoreProtocol,
    code: str,
    reasoning: str = "",
    org_id: str = "demo-public",
    min_samples: int = 2,
) -> RiskGateResponse:
    """Price an arbitrary snippet against this org's history — Prognosis without a git repo.

    Detects risk flags in the raw text exactly as the write-time recorder would, then prices
    them with the same `_price` Prognosis uses on a PR's diff.
    """
    flags = detect_risk_flags(code, reasoning)
    if not flags:
        return RiskGateResponse(verdict="clear")

    flag_stats = compute_flag_stats(store, org_id=org_id)
    priced: list[RiskGateFlag] = []
    for flag in flags:
        stat = flag_stats.get(flag)
        has_history = stat is not None and stat.decisions >= min_samples
        priced.append(
            RiskGateFlag(
                flag=flag,
                crash_rate=stat.crash_rate if (stat is not None and has_history) else None,
                sample_size=stat.decisions if stat is not None else 0,
            )
        )

    worst_rate, worst_flag, worst_samples = _price(flags, flag_stats, min_samples)
    verdict = "priced" if worst_rate is not None else "flagged"
    return RiskGateResponse(
        flags=priced,
        worst_flag=worst_flag,
        crash_rate=worst_rate,
        sample_size=worst_samples,
        verdict=verdict,
    )

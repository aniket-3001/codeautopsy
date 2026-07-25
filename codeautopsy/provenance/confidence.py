"""Attribution confidence — how sure are we this decision authored this crash?

A resolved autopsy is a claim: *this* AI decision wrote *this* crashing line. That claim is not
equally strong every time. Two things weaken it:

  1. **How we matched.** A decision recorded against the exact deployed commit is a direct hit.
     A decision reached by walking `git blame` back to an introducing commit is one inference
     removed — blame is reliable but not certain (a reformat or a moved block can mislead it).
  2. **How specific the decision's line range is.** A decision that claims a tight 5-line range
     around the crash is a sharper attribution than one whose range spans 200 lines and happens
     to contain the crash line near its edge.

`attribution_confidence` folds these into a single 0–1 score plus a factors dict that spells out
*why*, so the UI, the autopsy span, and the MCP payload can all show the same honest number
instead of implying every resolution is equally certain.
"""

from __future__ import annotations

from typing import Any

from codeautopsy.provenance.models import ResolveRequest, ResolveResponse

# Where the score lands on the human-readable band.
_HIGH = 0.80
_MEDIUM = 0.55

# How much a blame-derived match is discounted versus an exact-commit match.
_EXACT_WEIGHT = 1.0
_BLAME_WEIGHT = 0.80

# A range this wide (or wider) bottoms out the specificity term at _SPECIFICITY_FLOOR.
_WIDE_RANGE = 60
_SPECIFICITY_FLOOR = 0.50

# Composite weights for the range terms (must sum to 1.0).
_W_SPECIFICITY = 0.65
_W_POSITION = 0.35


def _label(score: float) -> str:
    if score >= _HIGH:
        return "high"
    if score >= _MEDIUM:
        return "medium"
    return "low"


def attribution_confidence(
    resp: ResolveResponse, req: ResolveRequest
) -> tuple[float | None, dict[str, Any] | None]:
    """Score how confidently `resp`'s decision is the author of `req`'s crashing line.

    Returns ``(score, factors)`` for a resolved response, or ``(None, None)`` when there is
    nothing to score (unresolved, or no record). `factors` always carries a ``label`` key so
    callers never have to re-derive the band.
    """
    if not resp.resolved or resp.record is None:
        return None, None

    rec = resp.record

    # 1. Match kind. The fast path records introducing_commit == the deployed commit; the blame
    #    path records the (possibly different) introducing commit. Either way, if they coincide
    #    the line genuinely originated in the deployed commit — an exact, un-inferred hit.
    exact = resp.introducing_commit == req.commit_sha
    match = "exact-commit" if exact else "git-blame"
    path_weight = _EXACT_WEIGHT if exact else _BLAME_WEIGHT

    # 2. Range specificity. width == 1 is maximally specific; a very wide range is a weak claim.
    width = max(1, rec.line_end - rec.line_start + 1)
    specificity = 1.0 - min(1.0, (width - 1) / _WIDE_RANGE) * (1.0 - _SPECIFICITY_FLOOR)

    # 3. Position of the crash within the range. Central lines are the strongest claim; a line at
    #    the very edge of a wide range is more likely coincidental containment.
    if width == 1:
        position = "single-line"
        centrality = 1.0
    else:
        mid = (rec.line_start + rec.line_end) / 2
        offset = abs(req.line - mid) / (width / 2)  # 0 at centre, 1 at either edge
        offset = min(1.0, max(0.0, offset))
        centrality = 1.0 - 0.30 * offset  # edges lose at most 30%
        position = "central" if offset <= 0.5 else "edge"

    score = round(path_weight * (_W_SPECIFICITY * specificity + _W_POSITION * centrality), 2)
    factors = {
        "label": _label(score),
        "match": match,
        "line_range": [rec.line_start, rec.line_end],
        "range_width": width,
        "position": position,
    }
    return score, factors

"""Reliability — the aggregate lens on the same provenance + incidents join.

Autopsy resolves one crash to one decision; Prognosis prices one PR's diff. Reliability
zooms out to the whole tenant: which AI tool/model actually ships the most crashes
(the leaderboard), and — without needing a git repo at all — what does an arbitrary code
snippet score against this org's own historical crash rates (the risk gate).

No new storage. Every number here is recomputed from `provenance` + `incidents` using the
exact same primitives Prognosis uses (`detect_risk_flags`, `compute_flag_stats`, `_price`).
"""

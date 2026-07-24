"""Auto-Heal loop — close the circle from crash to fix.

CodeAutopsy's other features answer *who* wrote the crashing line and *how risky* a
change is. Auto-Heal is the payoff: when the sample app crashes for real, a SigNoz alert
fires a webhook here, we open a `HealRun`, and a first-party Fix Bot (GitHub Actions)
patches OUR repo's seeded bug and opens a PR — the same `codeautopsy fix` the CLI runs,
now driven by a production signal instead of a human.

The Fix Bot only ever patches CodeAutopsy's own sample app, so this never touches customer
source (constraint D1). The dashboard's #/autoheal page polls `list_heal_runs` and renders
the timeline live.
"""

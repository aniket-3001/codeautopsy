# Fix Bot governance — autonomy levels

An agent that can change production code needs a clear, honest answer to "how far is it allowed to
go on its own?" CodeAutopsy answers it with explicit autonomy levels. The important property:
**the loop stops at a pull request. A human merges.** Nothing here auto-applies code to production.

## The levels

| Level | Name | What the system does | Human in the loop? | Where in the code |
|---|---|---|---|---|
| **L0** | Observe | Record every AI coding decision (reasoning + risk flags) as a decision span. No action taken. | n/a — passive | `recorder/` |
| **L1** | Attribute | On a crash, resolve the crashing line back to the decision that authored it, with an attribution-confidence score. Read-only insight. | Reads it | `enricher/`, `provenance/` |
| **L2** | Advise | Price a snippet's risk against real crash history (`prognose`), rank tools/models by crash rate (`leaderboard`). Still no code change. | Acts on advice | `reliability/`, `mcp/` |
| **L3** | Propose | Fix Bot writes a patch **and** a regression test, verifies the test fails-then-passes, commits to a **branch**, and (with `--push`) opens a **pull request**. | **Reviews & merges the PR** | `fixbot/` |
| **L4** | Auto-heal (gated) | A production SigNoz alert fires a webhook that drives L3 automatically — no human triggers it. It still stops at "Fix verified & PR opened". | **Reviews & merges the PR** | `autoheal/` |

There is deliberately **no L5 "auto-merge to production"**. The Fix Bot never runs `git merge` or
merges its own PR; the Auto-Heal loop's terminal state is *"Fix verified & PR opened"*
(`autoheal/core.py`). Shipping the change is always a human decision.

## Why the ceiling is the PR

* **Every patch is evidence-backed, not merged on faith.** The regression test must fail on the
  buggy code and pass on the patch before a PR is opened — but a passing test is necessary, not
  sufficient, for a human to want it in `main`.
* **Provenance is preserved for the reviewer.** The fix commit carries git trailers
  (`Codeautopsy-Decision-Id`, a traceparent back to the decision span) and the PR body explains
  the cause of death, the original reasoning, and the lesson learned — so the reviewer merges with
  full context.
* **Autonomy escalates with signal, not with trust.** L4 differs from L3 only in *who pulls the
  trigger* (a production alert vs a developer). The blast radius of a mistake is identical because
  both stop at the same gate.

## Defaults

* `codeautopsy fix` runs at **L3** and only opens a PR when you pass `--push`; without it, the fix
  stays on a local branch for you to inspect.
* The Auto-Heal loop (**L4**) is opt-in: it only runs where a SigNoz alert is wired to its webhook.

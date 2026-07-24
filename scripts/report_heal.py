"""Report a Fix Bot outcome back to the Auto-Heal loop.

Runs as the final step of .github/workflows/autoheal.yml: it reads the JSON line emitted by
`codeautopsy fix --json`, maps it to a heal-complete payload, and POSTs it (shared-secret
authed) to the callback URL the hosted API embedded in the repository_dispatch. Best-effort
by design — a heal run that can't phone home should never fail the workflow noisily.
"""

from __future__ import annotations

import json
import os
import sys

import httpx


def _load_result(path: str) -> dict:
    try:
        lines = [ln for ln in open(path, encoding="utf-8").read().splitlines() if ln.strip()]
    except FileNotFoundError:
        return {}
    # `codeautopsy fix --json` prints exactly one JSON line; take the last non-empty line to
    # be robust against any stray output ahead of it.
    for line in reversed(lines):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return {}


def main() -> int:
    result_path = sys.argv[1] if len(sys.argv) > 1 else "fix_result.json"
    callback_url = os.environ.get("HEAL_CALLBACK_URL", "").strip()
    secret = os.environ.get("HEAL_WEBHOOK_SECRET", "")
    org_id = os.environ.get("HEAL_ORG_ID", "demo-public") or "demo-public"

    if not callback_url:
        print("report_heal: no HEAL_CALLBACK_URL — nothing to report back to.")
        return 0

    result = _load_result(result_path)
    verified = bool(result.get("verified"))
    payload = {
        "org_id": org_id,
        "status": "succeeded" if verified else "failed",
        "pr_url": result.get("pr_url"),
        "branch": result.get("branch"),
        "explanation": result.get("explanation", ""),
        "lesson": result.get("lesson", ""),
        "detail": result.get("detail", "") or ("" if verified else "Fix Bot did not verify a fix."),
    }

    try:
        resp = httpx.post(
            callback_url, json=payload, headers={"X-Heal-Secret": secret}, timeout=30.0
        )
        print(f"report_heal: {resp.status_code} {resp.text[:200]}")
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        # Don't fail the workflow just because the callback is unreachable.
        print(f"report_heal: callback failed: {exc}")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

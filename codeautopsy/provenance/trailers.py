"""AI-provenance git trailers — provenance that lives in git history itself.

The provenance store is one copy of the truth; git history is another, and it is the one that
travels with the code forever, survives a dropped database, and shows up in `git log` and every
code-review UI. So when the Fix Bot commits a patch, it stamps the commit's footer with standard
git *trailers* recording which AI decision the patch descends from:

    Codeautopsy-Decision-Id: dec_ab12cd34
    Codeautopsy-Traceparent: 00-7b5b1b39741a991f073d59e245fb7575-00f067aa0ba902b7-01
    Codeautopsy-Autopsy: app/payment.py:42@abc123def456

`Codeautopsy-Traceparent` is a W3C traceparent pointing at the *decision* span in SigNoz, so a
reviewer reading the commit a year later can jump straight to the agent's original reasoning.

This module both writes those trailers (`format_trailers`) and reads them back out of any commit
(`read_commit_trailers`), so provenance can be recovered from git alone — no store required.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

TRAILER_DECISION_ID = "Codeautopsy-Decision-Id"
TRAILER_TRACEPARENT = "Codeautopsy-Traceparent"
TRAILER_COORDINATE = "Codeautopsy-Autopsy"

_HEX32 = re.compile(r"\A[0-9a-f]{32}\Z")
_HEX16 = re.compile(r"\A[0-9a-f]{16}\Z")
# A traceparent we emit: version 00, 32-hex trace, 16-hex span, flags.
_TRACEPARENT = re.compile(r"\A00-(?P<trace>[0-9a-f]{32})-(?P<span>[0-9a-f]{16})-[0-9a-f]{2}\Z")
# One trailer line: "Codeautopsy-Foo: value".
_TRAILER_LINE = re.compile(r"^(?P<key>Codeautopsy-[A-Za-z-]+):[ \t]*(?P<value>.*?)[ \t]*$")


def build_traceparent(trace_id: str | None, span_id: str | None) -> str | None:
    """A W3C traceparent for the decision span, or None if the ids aren't well-formed hex.

    Trace/span ids come straight from recorded spans, but a malformed one must never produce a
    trailer that looks valid but points nowhere — so we validate before emitting.
    """
    if not trace_id or not span_id:
        return None
    trace_id, span_id = trace_id.lower(), span_id.lower()
    if not _HEX32.match(trace_id) or not _HEX16.match(span_id):
        return None
    return f"00-{trace_id}-{span_id}-01"


def format_trailers(
    *,
    decision_id: str = "",
    trace_id: str | None = None,
    span_id: str | None = None,
    coordinate: str = "",
) -> str:
    """Render the CodeAutopsy trailer block (newline-joined), omitting any part we don't have.

    Returns "" when there is nothing to stamp, so callers can safely append it unconditionally.
    """
    lines: list[str] = []
    if decision_id:
        lines.append(f"{TRAILER_DECISION_ID}: {decision_id}")
    traceparent = build_traceparent(trace_id, span_id)
    if traceparent:
        lines.append(f"{TRAILER_TRACEPARENT}: {traceparent}")
    if coordinate:
        lines.append(f"{TRAILER_COORDINATE}: {coordinate}")
    return "\n".join(lines)


def parse_trailers(message: str) -> dict:
    """Extract CodeAutopsy provenance from a commit message's trailers.

    Returns a dict with any of ``decision_id`` / ``traceparent`` / ``coordinate`` that are
    present, plus ``trace_id`` / ``span_id`` decomposed from the traceparent when it is valid.
    An empty dict means the commit carries no CodeAutopsy provenance.
    """
    found: dict[str, str] = {}
    for line in message.splitlines():
        m = _TRAILER_LINE.match(line)
        if not m:
            continue
        key, value = m.group("key"), m.group("value")
        if key == TRAILER_DECISION_ID:
            found["decision_id"] = value
        elif key == TRAILER_TRACEPARENT:
            found["traceparent"] = value
        elif key == TRAILER_COORDINATE:
            found["coordinate"] = value

    tp = found.get("traceparent")
    if tp:
        tm = _TRACEPARENT.match(tp)
        if tm:
            found["trace_id"] = tm.group("trace")
            found["span_id"] = tm.group("span")
    return found


def read_commit_trailers(repo: str | Path, commit_sha: str) -> dict:
    """Read CodeAutopsy provenance trailers out of a single commit via git.

    Returns the same shape as `parse_trailers`; an empty dict if the commit has none or git
    can't read it (bad sha, not a repo) — recovery from history should degrade quietly.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%B", commit_sha],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return {}
    return parse_trailers(proc.stdout)

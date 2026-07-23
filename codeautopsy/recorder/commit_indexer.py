"""Binds queued dev-time decisions to a real commit after `git commit`.

Run via `codeautopsy index-commit` (or a git post-commit hook). For each pending decision,
re-blames the file at HEAD to recover the current (possibly shifted) line numbers, then
writes a ProvenanceRecord keyed on the real commit SHA.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from codeautopsy.provenance.indexer import blame_origin
from codeautopsy.provenance.models import ProvenanceRecord
from codeautopsy.provenance.store import ProvenanceStore
from codeautopsy.recorder.pending import clear_pending, read_pending


def head_sha(repo_root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def index_pending_at_head(repo_root: Path, store: ProvenanceStore) -> int:
    """Bind every pending decision to HEAD, using blame to confirm the current line."""
    pending = read_pending(repo_root)
    if not pending:
        return 0

    head = head_sha(repo_root)
    indexed = 0
    for item in pending:
        origin = blame_origin(repo_root, item["file_path"], item["line_start"], head)
        line_start = origin[1] if origin else item["line_start"]
        line_end = line_start + (item["line_end"] - item["line_start"])

        record = ProvenanceRecord(
            commit_sha=head,
            file_path=item["file_path"],
            line_start=line_start,
            line_end=line_end,
            decision_span_id=item["decision_span_id"],
            decision_trace_id=item["decision_trace_id"],
            session_id=item["session_id"],
            reasoning_summary=item.get("reasoning_summary", ""),
            risk_flags=item.get("risk_flags", []),
            decision_id=item.get("decision_id", ""),
            tool=item.get("tool", "claude-code"),
        )
        store.add(record)
        indexed += 1

    clear_pending(repo_root)
    return indexed

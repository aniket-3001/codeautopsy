"""Pending decision queue.

Decisions are captured at edit-time, before we know which commit they'll land in. They're
queued here as JSONL under `.codeautopsy/`, then bound to a real commit by the indexer
(codeautopsy.recorder.commit_indexer) after `git commit`.
"""

from __future__ import annotations

import json
from pathlib import Path


def _codeautopsy_dir(repo_root: Path) -> Path:
    d = repo_root / ".codeautopsy"
    d.mkdir(exist_ok=True)
    return d


def pending_path(repo_root: Path) -> Path:
    return _codeautopsy_dir(repo_root) / "pending_decisions.jsonl"


def append_pending(repo_root: Path, record: dict) -> None:
    with pending_path(repo_root).open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def read_pending(repo_root: Path) -> list[dict]:
    path = pending_path(repo_root)
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def clear_pending(repo_root: Path) -> None:
    path = pending_path(repo_root)
    if path.exists():
        path.unlink()

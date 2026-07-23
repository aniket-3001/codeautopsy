"""Write-time risk-flag detection + reasoning extraction from the agent's own words.

Heuristic, not ML — deliberately simple regexes so the detection is explainable in a demo
("why did this get flagged?" has a one-line answer) and has zero external dependencies.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

RISK_PATTERNS: dict[str, re.Pattern] = {
    "assumed_valid_input": re.compile(
        r"\bassum\w*\b[^.]{0,40}\b(valid|always|clean|correct)\b"
        r"|\b(valid|always|clean|correct)\b[^.]{0,40}\bassum\w*\b",
        re.I,
    ),
    "skipped_tests": re.compile(
        r"\bskip(ped|ping)?\b[^.]{0,30}\btests?\b"
        r"|\bwithout\b[^.]{0,20}\btests?\b"
        r"|\btests?\b[^.]{0,20}\b(later|todo|separately)\b",
        re.I,
    ),
    "uncertainty": re.compile(
        r"\b(i think|not sure|might|probably|should work|hopefully|i believe|not certain)\b",
        re.I,
    ),
    "todo_left": re.compile(r"\bTODO\b|\bFIXME\b|\bXXX\b"),
    "swallowed_exception": re.compile(
        r"except[^:]*:\s*pass\b|\bcatch\b[^.]{0,30}\b(ignore|swallow)\w*\b", re.I
    ),
    "hardcoded_value": re.compile(r"\bhardcod\w*\b", re.I),
    "disabled_check": re.compile(
        r"\b(disab\w*|remov\w*|bypass\w*)\b[^.]{0,30}\b(check|valid\w*|test)\b", re.I
    ),
}


def detect_risk_flags(*texts: str) -> list[str]:
    """Scan reasoning + written code for risky patterns. Returns a sorted, deduped list."""
    haystack = "\n".join(t for t in texts if t)
    if not haystack:
        return []
    return sorted({flag for flag, pattern in RISK_PATTERNS.items() if pattern.search(haystack)})


def extract_last_assistant_reasoning(transcript_path: str | Path, max_chars: int = 500) -> str:
    """Best-effort: pull the most recent assistant text (not tool-call blocks) as the
    reasoning behind the tool call that just happened.

    Reads the Claude Code session transcript (JSONL) backwards and returns the first
    assistant text block found. Never raises — this runs inside a hook subprocess, and a
    crashing hook breaks the user's live session.
    """
    try:
        path = Path(transcript_path)
        if not path.exists():
            return ""
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "assistant":
                continue
            message = entry.get("message") or {}
            content = message.get("content")
            if not isinstance(content, list):
                continue
            texts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            joined = " ".join(t.strip() for t in texts if t.strip())
            if joined:
                return joined[:max_chars]
    except Exception:  # noqa: BLE001 — hooks must never crash the user's session
        return ""
    return ""

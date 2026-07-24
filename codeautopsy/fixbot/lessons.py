"""Lessons memory — the Fix Bot remembers, and replays, what it has already learned.

Every verified fix already yields a one-sentence *lesson* ("always validate external input
before int()"). Until now that sentence was written into the commit message and forgotten. This
module gives it a memory:

  * `lesson_fingerprint` reduces a crash to the *class* of bug it belongs to — specific values
    (the offending key, a size, a line number) are stripped, so a KeyError on `'code'` and one on
    `'coupon'` in the same file collapse to a single class.
  * `record_lesson` persists the lesson under that fingerprint after a fix verifies.
  * `recall_lesson` looks it up again — a pure store read, **no LLM call**. When the same class of
    bug recurs, the Fix Bot can surface the lesson it already learned instantly, and feed it back
    into its own prompt so it doesn't re-learn the same thing from scratch.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from codeautopsy.provenance.models import LessonRecord

if TYPE_CHECKING:
    from codeautopsy.provenance.store import ProvenanceStoreProtocol

__all__ = ["LessonRecord", "lesson_fingerprint", "record_lesson", "recall_lesson"]

# Strip specific values so a *class* of bug fingerprints stably: quoted literals and bare numbers.
_NOISE = re.compile(r"'[^']*'|\"[^\"]*\"|\b\d+\b")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def lesson_fingerprint(cause_of_death: str, file_path: str, risk_flags: list[str]) -> str:
    """A stable 16-hex id for the *class* of bug, so a recurrence maps to the same lesson."""
    cause = _NOISE.sub("", (cause_of_death or "").lower())
    cause = re.sub(r"\s+", " ", cause).strip()
    flags = ",".join(sorted(risk_flags or []))
    basis = f"{file_path}|{cause}|{flags}"
    return hashlib.sha1(basis.encode()).hexdigest()[:16]


def record_lesson(
    store: ProvenanceStoreProtocol,
    *,
    lesson: str,
    cause_of_death: str,
    file_path: str,
    risk_flags: list[str],
    decision_id: str = "",
    patch_summary: str = "",
    org_id: str = "demo-public",
) -> LessonRecord:
    """Persist (or bump the recurrence count of) the lesson for this class of bug."""
    record = LessonRecord(
        org_id=org_id,
        fingerprint=lesson_fingerprint(cause_of_death, file_path, risk_flags),
        lesson=lesson,
        decision_id=decision_id,
        file_path=file_path,
        cause_of_death=cause_of_death,
        patch_summary=patch_summary,
        updated_at=_now(),
    )
    store.record_lesson(record)
    return record


def recall_lesson(
    store: ProvenanceStoreProtocol,
    *,
    cause_of_death: str,
    file_path: str,
    risk_flags: list[str],
    org_id: str = "demo-public",
) -> LessonRecord | None:
    """Look up a previously learned lesson for this class of bug — a pure read, no LLM call."""
    fp = lesson_fingerprint(cause_of_death, file_path, risk_flags)
    return store.find_lesson(org_id, fp)

"""Tests for the Fix Bot lessons memory (`codeautopsy.fixbot.lessons` + store persistence)."""

from __future__ import annotations

import tempfile

import pytest

from codeautopsy.fixbot.lessons import lesson_fingerprint, recall_lesson, record_lesson
from codeautopsy.provenance.store import ProvenanceStore


@pytest.fixture
def store() -> ProvenanceStore:
    return ProvenanceStore(tempfile.NamedTemporaryFile(suffix=".db", delete=False).name)


def test_fingerprint_ignores_specific_values() -> None:
    # Same class of bug, different offending key / number -> same fingerprint.
    a = lesson_fingerprint("unhandled KeyError: 'code'", "app/pay.py", ["unvalidated-input"])
    b = lesson_fingerprint("unhandled KeyError: 'coupon'", "app/pay.py", ["unvalidated-input"])
    assert a == b


def test_fingerprint_separates_different_files_or_flags() -> None:
    base = lesson_fingerprint("KeyError", "app/pay.py", ["unvalidated-input"])
    assert base != lesson_fingerprint("KeyError", "app/other.py", ["unvalidated-input"])
    assert base != lesson_fingerprint("KeyError", "app/pay.py", ["off-by-one"])


def test_record_then_recall_roundtrip(store: ProvenanceStore) -> None:
    record_lesson(
        store,
        lesson="validate external input before int()",
        cause_of_death="invalid literal for int() with base 10: 'abc'",
        file_path="app/pay.py",
        risk_flags=["unvalidated-input"],
        decision_id="dec_1",
        patch_summary="guarded the cast",
    )
    hit = recall_lesson(
        store,
        # Same class of bug, different offending value + base number -> same fingerprint.
        cause_of_death="invalid literal for int() with base 16: 'xyz'",
        file_path="app/pay.py",
        risk_flags=["unvalidated-input"],
    )
    assert hit is not None
    assert hit.lesson == "validate external input before int()"
    assert hit.times_seen == 1


def test_recurrence_bumps_times_seen_not_duplicates(store: ProvenanceStore) -> None:
    for _ in range(3):
        record_lesson(
            store,
            lesson="validate input",
            cause_of_death="KeyError: 'code'",
            file_path="app/pay.py",
            risk_flags=["unvalidated-input"],
        )
    hit = recall_lesson(
        store,
        cause_of_death="KeyError: 'code'",
        file_path="app/pay.py",
        risk_flags=["unvalidated-input"],
    )
    assert hit is not None and hit.times_seen == 3
    assert len(store.list_lessons()) == 1


def test_recall_miss_is_none(store: ProvenanceStore) -> None:
    assert (
        recall_lesson(store, cause_of_death="nope", file_path="x.py", risk_flags=[]) is None
    )


def test_lessons_are_org_scoped(store: ProvenanceStore) -> None:
    record_lesson(
        store,
        lesson="tenant-a lesson",
        cause_of_death="KeyError",
        file_path="app/pay.py",
        risk_flags=[],
        org_id="tenant-a",
    )
    assert recall_lesson(
        store, cause_of_death="KeyError", file_path="app/pay.py", risk_flags=[]
    ) is None  # default org can't see tenant-a
    assert store.list_lessons("tenant-a")
    assert store.list_lessons("demo-public") == []

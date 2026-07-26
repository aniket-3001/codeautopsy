"""Tests for the Coroner CLI — never exercised before (0% coverage, never run live)."""

from __future__ import annotations

import httpx
from typer.testing import CliRunner

import codeautopsy.cli.main as cli_main
from codeautopsy.fixbot.core import FixBotError
from codeautopsy.fixbot.models import FixBotResult

runner = CliRunner()


def _resolved_json():
    return {
        "resolved": True,
        "introducing_commit": "abc123def456",
        "detail": "matched decision recorded at the deployed commit",
        "record": {
            "commit_sha": "abc123def456",
            "file_path": "app/payment.py",
            "line_start": 40,
            "line_end": 45,
            "decision_span_id": "e91ca75cd1ae81e4",
            "decision_trace_id": "c51641b768a8a67ea979f9005ade2f55",
            "session_id": "sess_test",
            "reasoning_summary": "assuming the input is always valid",
            "risk_flags": ["assumed_valid_input"],
            "model": "",
            "tool": "claude-code",
            "decision_id": "dec_7f3a",
            "created_at": "2026-07-23T10:00:00+00:00",
        },
    }


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        return self._payload


def test_autopsy_resolved(monkeypatch):
    monkeypatch.setattr(cli_main.httpx, "post", lambda *a, **kw: _FakeResponse(_resolved_json()))
    result = runner.invoke(cli_main.app, ["autopsy", "abc123def456", "app/payment.py", "42"])
    assert result.exit_code == 0
    assert "assuming the input is always valid" in result.stdout
    assert "dec_7f3a" in result.stdout


def test_autopsy_unresolved(monkeypatch):
    payload = {"resolved": False, "detail": "no matching provenance and no repo to blame"}
    monkeypatch.setattr(cli_main.httpx, "post", lambda *a, **kw: _FakeResponse(payload))
    result = runner.invoke(cli_main.app, ["autopsy", "abc123", "app/payment.py", "42"])
    assert result.exit_code == 1
    assert "Not resolved" in result.stdout


def test_autopsy_service_unreachable(monkeypatch):
    def fake_post(*a, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(cli_main.httpx, "post", fake_post)
    result = runner.invoke(cli_main.app, ["autopsy", "abc123", "app/payment.py", "42"])
    assert result.exit_code == 2
    assert "unreachable" in result.stdout


def test_fix_verified(monkeypatch):
    fake_result = FixBotResult(
        verified=True,
        explanation="Guard int(code) with a try/except and default to 0.",
        lesson="Never trust an external discount code to be numeric.",
        branch="codeautopsy/fix-dec_7f3a",
        commit_sha="deadbeefcafe",
        pr_url=None,
        detail="fix verified by regression test and committed",
    )
    monkeypatch.setattr(cli_main, "run_fixbot", lambda *a, **kw: fake_result)
    result = runner.invoke(cli_main.app, ["fix", "abc123", "app/payment.py", "42"])
    assert result.exit_code == 0
    assert "verified & committed" in result.stdout
    assert "deadbeefcafe" in result.stdout
    assert "not pushed" in result.stdout


def test_fix_verification_failed(monkeypatch):
    fake_result = FixBotResult(
        verified=False,
        test_output="AssertionError: expected 0, got None",
        detail="regression test failed against the proposed fix — nothing committed.",
    )
    monkeypatch.setattr(cli_main, "run_fixbot", lambda *a, **kw: fake_result)
    result = runner.invoke(cli_main.app, ["fix", "abc123", "app/payment.py", "42"])
    assert result.exit_code == 1
    assert "verification failed" in result.stdout


def test_fix_bot_error(monkeypatch):
    def fake_run_fixbot(*a, **kw):
        raise FixBotError("working tree is not clean")

    monkeypatch.setattr(cli_main, "run_fixbot", fake_run_fixbot)
    result = runner.invoke(cli_main.app, ["fix", "abc123", "app/payment.py", "42"])
    assert result.exit_code == 2
    assert "Fix Bot failed" in result.stdout


def test_prognose_clean_diff(monkeypatch, tmp_path):
    from codeautopsy.config import Settings
    from codeautopsy.prognosis.models import PrognosisReport

    settings = Settings(CODEAUTOPSY_PROVENANCE_DB=str(tmp_path / "p.db"))
    monkeypatch.setattr(cli_main, "get_settings", lambda: settings)
    monkeypatch.setattr(
        cli_main,
        "scan",
        lambda *a, **kw: PrognosisReport(base_ref="main", head_ref="HEAD", lines_scanned=3),
    )
    result = runner.invoke(cli_main.app, ["prognose", "main", "--repo", str(tmp_path)])
    assert result.exit_code == 0
    assert "Clean bill of health" in result.stdout


def test_prognose_reports_priced_findings_and_posts_comment(monkeypatch, tmp_path):
    from codeautopsy.config import Settings
    from codeautopsy.prognosis.models import LineFinding, PrognosisReport

    settings = Settings(CODEAUTOPSY_PROVENANCE_DB=str(tmp_path / "p.db"))
    monkeypatch.setattr(cli_main, "get_settings", lambda: settings)
    report = PrognosisReport(
        base_ref="main",
        head_ref="HEAD",
        lines_scanned=1,
        findings=[
            LineFinding(
                file_path="app.py", line=10, risk_flags=["assumed_valid_input"],
                decision_id="d1", source="decision", crash_rate=0.75,
                worst_flag="assumed_valid_input", sample_size=4,
            )
        ],
    )
    monkeypatch.setattr(cli_main, "scan", lambda *a, **kw: report)
    monkeypatch.setattr(cli_main, "post_comment", lambda *a, **kw: "https://github.com/x/y/pull/1")

    result = runner.invoke(
        cli_main.app, ["prognose", "main", "--repo", str(tmp_path), "--comment"]
    )
    assert result.exit_code == 0
    assert "app.py:10" in result.stdout
    assert "Posted to PR" in result.stdout


def test_prognose_fail_on_risk_exits_nonzero(monkeypatch, tmp_path):
    from codeautopsy.config import Settings
    from codeautopsy.prognosis.models import LineFinding, PrognosisReport

    settings = Settings(CODEAUTOPSY_PROVENANCE_DB=str(tmp_path / "p.db"))
    monkeypatch.setattr(cli_main, "get_settings", lambda: settings)
    report = PrognosisReport(
        base_ref="main",
        head_ref="HEAD",
        lines_scanned=1,
        findings=[
            LineFinding(
                file_path="app.py", line=10, risk_flags=["assumed_valid_input"],
                source="decision", crash_rate=0.75, worst_flag="assumed_valid_input",
                sample_size=4,
            )
        ],
    )
    monkeypatch.setattr(cli_main, "scan", lambda *a, **kw: report)

    result = runner.invoke(
        cli_main.app, ["prognose", "main", "--repo", str(tmp_path), "--fail-on-risk"]
    )
    assert result.exit_code == 1


def test_prognose_failure_exits_cleanly(monkeypatch, tmp_path):
    from codeautopsy.config import Settings
    from codeautopsy.prognosis.core import PrognosisError

    settings = Settings(CODEAUTOPSY_PROVENANCE_DB=str(tmp_path / "p.db"))
    monkeypatch.setattr(cli_main, "get_settings", lambda: settings)

    def fake_scan(*a, **kw):
        raise PrognosisError("not a git repository")

    monkeypatch.setattr(cli_main, "scan", fake_scan)
    result = runner.invoke(cli_main.app, ["prognose", "main", "--repo", str(tmp_path)])
    assert result.exit_code == 2
    assert "Prognosis failed" in result.stdout


def test_report_resolved_with_lesson(monkeypatch):
    from codeautopsy.fixbot.models import Genealogy
    from codeautopsy.provenance.models import LessonRecord

    genealogy = Genealogy(
        file_path="app/payment.py",
        line=42,
        commit_sha="abc123def456",
        file_content="int(x)\n",
        reasoning_summary="assuming the input is always valid",
        risk_flags=["assumed_valid_input"],
        decision_id="dec_7f3a",
        exc_type="ValueError",
        exc_message="bad input",
        cause_of_death="invalid value — unvalidated input",
        confidence=1.0,
        confidence_factors={"label": "high", "match": "exact-commit"},
    )
    lesson = LessonRecord(
        fingerprint="fp1", lesson="Validate before int().", times_seen=2, patch_summary="try/except"
    )
    monkeypatch.setattr("codeautopsy.fixbot.core.build_genealogy", lambda *a, **k: genealogy)
    monkeypatch.setattr("codeautopsy.fixbot.lessons.recall_lesson", lambda *a, **k: lesson)
    monkeypatch.setattr("codeautopsy.provenance.store.make_store", lambda settings: object())

    result = runner.invoke(cli_main.app, ["report", "abc123def456", "app/payment.py", "42"])

    assert result.exit_code == 0
    assert "dec_7f3a" in result.stdout
    assert "Validate before int()." in result.stdout
    assert "Seen **2x**" in result.stdout


def test_report_unresolved_degrades_gracefully(monkeypatch):
    from codeautopsy.fixbot.models import Genealogy

    genealogy = Genealogy(
        file_path="app/other.py", line=9, commit_sha="deadbeef", file_content="x = 1\n"
    )
    monkeypatch.setattr("codeautopsy.fixbot.core.build_genealogy", lambda *a, **k: genealogy)
    monkeypatch.setattr("codeautopsy.fixbot.lessons.recall_lesson", lambda *a, **k: None)
    monkeypatch.setattr("codeautopsy.provenance.store.make_store", lambda settings: object())

    result = runner.invoke(cli_main.app, ["report", "deadbeef", "app/other.py", "9"])

    assert result.exit_code == 0
    assert "No decision indexed for this line" in result.stdout
    assert "No lesson recorded yet" in result.stdout


def test_report_fixbot_error(monkeypatch):
    def _raise(*a, **k):
        raise FixBotError("does not exist under target repo")

    monkeypatch.setattr("codeautopsy.fixbot.core.build_genealogy", _raise)

    result = runner.invoke(cli_main.app, ["report", "abc123", "nope.py", "1"])

    assert result.exit_code == 2
    assert "Could not assemble the postmortem" in result.stdout


def test_report_writes_to_file(monkeypatch, tmp_path):
    from codeautopsy.fixbot.models import Genealogy

    genealogy = Genealogy(
        file_path="app/other.py", line=9, commit_sha="deadbeef", file_content="x = 1\n"
    )
    monkeypatch.setattr("codeautopsy.fixbot.core.build_genealogy", lambda *a, **k: genealogy)
    monkeypatch.setattr("codeautopsy.fixbot.lessons.recall_lesson", lambda *a, **k: None)
    monkeypatch.setattr("codeautopsy.provenance.store.make_store", lambda settings: object())
    out_file = tmp_path / "postmortem.md"

    result = runner.invoke(
        cli_main.app, ["report", "deadbeef", "app/other.py", "9", "--out", str(out_file)]
    )

    assert result.exit_code == 0
    assert "Postmortem written to" in result.stdout
    assert out_file.exists()
    assert "# Postmortem" in out_file.read_text(encoding="utf-8")


def test_index_commit(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_main, "index_pending_at_head", lambda repo_root, store: 3)
    result = runner.invoke(cli_main.app, ["index-commit", "--repo", str(tmp_path)])
    assert result.exit_code == 0
    assert "Indexed 3 decision(s)" in result.stdout


def test_status(monkeypatch, tmp_path):
    from codeautopsy.config import Settings

    settings = Settings(CODEAUTOPSY_PROVENANCE_DB=str(tmp_path / "p.db"))
    monkeypatch.setattr(cli_main, "get_settings", lambda: settings)
    result = runner.invoke(cli_main.app, ["status"])
    assert result.exit_code == 0
    assert "CodeAutopsy status" in result.stdout
    assert "0" in result.stdout


def test_parse_lines_single_value():
    assert cli_main._parse_lines("42") == (42, 42)


def test_parse_lines_range():
    assert cli_main._parse_lines("40-46") == (40, 46)


def test_parse_lines_swaps_reversed_range():
    assert cli_main._parse_lines("46-40") == (40, 46)


def test_autopsy_resolved_with_confidence(monkeypatch):
    payload = _resolved_json()
    payload["confidence"] = 0.92
    payload["confidence_factors"] = {"label": "high", "match": "exact-commit"}
    monkeypatch.setattr(cli_main.httpx, "post", lambda *a, **kw: _FakeResponse(payload))
    result = runner.invoke(cli_main.app, ["autopsy", "abc123def456", "app/payment.py", "42"])
    assert result.exit_code == 0
    assert "confidence:" in result.stdout
    assert "92%" in result.stdout


def test_provenance_found(monkeypatch, tmp_path):
    trailers = {
        "decision_id": "dec_7f3a",
        "coordinate": "app/payment.py:42@abc123def456",
        "traceparent": "00-7b5b1b39741a991f073d59e245fb7575-00f067aa0ba902b7-01",
        "trace_id": "7b5b1b39741a991f073d59e245fb7575",
        "span_id": "00f067aa0ba902b7",
    }
    monkeypatch.setattr(
        "codeautopsy.provenance.trailers.read_commit_trailers", lambda repo, commit: trailers
    )
    result = runner.invoke(
        cli_main.app, ["provenance", "abc123def456", "--repo", str(tmp_path)]
    )
    assert result.exit_code == 0
    assert "dec_7f3a" in result.stdout
    assert "app/payment.py:42@abc123def456" in result.stdout


def test_provenance_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "codeautopsy.provenance.trailers.read_commit_trailers", lambda repo, commit: {}
    )
    result = runner.invoke(cli_main.app, ["provenance", "deadbeef", "--repo", str(tmp_path)])
    assert result.exit_code == 1
    assert "No CodeAutopsy provenance trailers" in result.stdout


def test_lessons_empty(monkeypatch):
    class _EmptyStore:
        def list_lessons(self, org_id):
            return []

    monkeypatch.setattr("codeautopsy.provenance.store.make_store", lambda settings: _EmptyStore())
    result = runner.invoke(cli_main.app, ["lessons"])
    assert result.exit_code == 0
    assert "No lessons learned yet" in result.stdout


def test_lessons_lists_learned(monkeypatch):
    from codeautopsy.provenance.models import LessonRecord

    learned = [
        LessonRecord(
            fingerprint="fp1",
            lesson="Validate before int().",
            times_seen=3,
            cause_of_death="invalid value — unvalidated input",
            patch_summary="try/except",
        )
    ]

    class _Store:
        def list_lessons(self, org_id):
            return learned

    monkeypatch.setattr("codeautopsy.provenance.store.make_store", lambda settings: _Store())
    result = runner.invoke(cli_main.app, ["lessons"])
    assert result.exit_code == 0
    assert "Validate before int()." in result.stdout
    assert "3" in result.stdout


def test_recall_hit(monkeypatch):
    from codeautopsy.fixbot.models import Genealogy
    from codeautopsy.provenance.models import LessonRecord

    genealogy = Genealogy(
        file_path="app/payment.py", line=42, commit_sha="abc123def456", file_content="int(x)\n",
        cause_of_death="invalid value — unvalidated input", risk_flags=["assumed_valid_input"],
    )
    hit = LessonRecord(
        fingerprint="fp1", lesson="Validate before int().", times_seen=2, patch_summary="try/except"
    )
    monkeypatch.setattr("codeautopsy.fixbot.core.build_genealogy", lambda *a, **k: genealogy)
    monkeypatch.setattr("codeautopsy.fixbot.lessons.recall_lesson", lambda *a, **k: hit)
    monkeypatch.setattr("codeautopsy.provenance.store.make_store", lambda settings: object())

    result = runner.invoke(cli_main.app, ["recall", "abc123def456", "app/payment.py", "42"])

    assert result.exit_code == 0
    assert "Validate before int()." in result.stdout
    assert "replayed from memory" in result.stdout


def test_recall_no_lesson(monkeypatch):
    from codeautopsy.fixbot.models import Genealogy

    genealogy = Genealogy(
        file_path="app/other.py", line=9, commit_sha="deadbeef", file_content="x = 1\n"
    )
    monkeypatch.setattr("codeautopsy.fixbot.core.build_genealogy", lambda *a, **k: genealogy)
    monkeypatch.setattr("codeautopsy.fixbot.lessons.recall_lesson", lambda *a, **k: None)
    monkeypatch.setattr("codeautopsy.provenance.store.make_store", lambda settings: object())

    result = runner.invoke(cli_main.app, ["recall", "deadbeef", "app/other.py", "9"])

    assert result.exit_code == 1
    assert "No lesson in memory" in result.stdout


def test_fix_verified_with_prior_lesson(monkeypatch):
    fake_result = FixBotResult(
        verified=True,
        explanation="Guard int(code) with a try/except and default to 0.",
        lesson="Never trust an external discount code to be numeric.",
        branch="codeautopsy/fix-dec_7f3a",
        commit_sha="deadbeefcafe",
        prior_lesson="Always validate external input before int().",
        times_seen=3,
    )
    monkeypatch.setattr(cli_main, "run_fixbot", lambda *a, **kw: fake_result)
    result = runner.invoke(cli_main.app, ["fix", "abc123", "app/payment.py", "42"])
    assert result.exit_code == 0
    assert "recalled from memory" in result.stdout
    assert "seen 3x" in result.stdout


def test_fix_json_verified(monkeypatch):
    fake_result = FixBotResult(verified=True, branch="codeautopsy/fix-dec_7f3a", commit_sha="deadbeef")
    monkeypatch.setattr(cli_main, "run_fixbot", lambda *a, **kw: fake_result)
    result = runner.invoke(cli_main.app, ["fix", "abc123", "app/payment.py", "42", "--json"])
    assert result.exit_code == 0
    assert '"verified":true' in result.stdout.replace(" ", "")


def test_fix_json_bot_error(monkeypatch):
    def fake_run_fixbot(*a, **kw):
        raise FixBotError("working tree is not clean")

    monkeypatch.setattr(cli_main, "run_fixbot", fake_run_fixbot)
    result = runner.invoke(cli_main.app, ["fix", "abc123", "app/payment.py", "42", "--json"])
    assert result.exit_code == 2
    assert '"verified":false' in result.stdout.replace(" ", "")
    assert "working tree is not clean" in result.stdout


def test_prognose_comment_could_not_post(monkeypatch, tmp_path):
    from codeautopsy.config import Settings
    from codeautopsy.prognosis.models import PrognosisReport

    settings = Settings(CODEAUTOPSY_PROVENANCE_DB=str(tmp_path / "p.db"))
    monkeypatch.setattr(cli_main, "get_settings", lambda: settings)
    monkeypatch.setattr(
        cli_main,
        "scan",
        lambda *a, **kw: PrognosisReport(base_ref="main", head_ref="HEAD", lines_scanned=1),
    )
    monkeypatch.setattr(cli_main, "post_comment", lambda *a, **kw: None)
    result = runner.invoke(
        cli_main.app, ["prognose", "main", "--repo", str(tmp_path), "--comment"]
    )
    assert result.exit_code == 0
    assert "Could not post to PR" in result.stdout


def test_record_local(monkeypatch, tmp_path):
    from codeautopsy.config import Settings

    settings = Settings(CODEAUTOPSY_PROVENANCE_DB=str(tmp_path / "p.db"))
    monkeypatch.setattr(cli_main, "get_settings", lambda: settings)
    result = runner.invoke(
        cli_main.app,
        [
            "record",
            "--commit", "abc123def456",
            "--file", "app/checkout.py",
            "--lines", "40-46",
            "--reasoning", "trusting the discount code is numeric",
            "--risk-flag", "assumed_valid_input",
            "--tool", "claude-code",
        ],
    )
    assert result.exit_code == 0
    assert "Recorded" in result.stdout
    assert "(local" in result.stdout


def test_record_local_defaults_risk_source_to_heuristic(monkeypatch, tmp_path):
    from codeautopsy.config import Settings
    from codeautopsy.provenance.store import ProvenanceStore

    db = tmp_path / "p.db"
    settings = Settings(CODEAUTOPSY_PROVENANCE_DB=str(db))
    monkeypatch.setattr(cli_main, "get_settings", lambda: settings)
    result = runner.invoke(
        cli_main.app,
        [
            "record",
            "--commit", "abc123def456",
            "--file", "app/checkout.py",
            "--lines", "40-46",
        ],
    )
    assert result.exit_code == 0
    rec = ProvenanceStore(db).all()[0]
    assert rec.risk_source == "heuristic"


def test_record_local_accepts_ai_judge_risk_source(monkeypatch, tmp_path):
    from codeautopsy.config import Settings
    from codeautopsy.provenance.store import ProvenanceStore

    db = tmp_path / "p.db"
    settings = Settings(CODEAUTOPSY_PROVENANCE_DB=str(db))
    monkeypatch.setattr(cli_main, "get_settings", lambda: settings)
    result = runner.invoke(
        cli_main.app,
        [
            "record",
            "--commit", "abc123def456",
            "--file", "app/checkout.py",
            "--lines", "40-46",
            "--risk-source", "ai_judge",
        ],
    )
    assert result.exit_code == 0
    rec = ProvenanceStore(db).all()[0]
    assert rec.risk_source == "ai_judge"


def test_record_rejects_invalid_risk_source(monkeypatch, tmp_path):
    from codeautopsy.config import Settings

    settings = Settings(CODEAUTOPSY_PROVENANCE_DB=str(tmp_path / "p.db"))
    monkeypatch.setattr(cli_main, "get_settings", lambda: settings)
    result = runner.invoke(
        cli_main.app,
        [
            "record",
            "--commit", "abc123def456",
            "--file", "app/checkout.py",
            "--lines", "40-46",
            "--risk-source", "vibes",
        ],
    )
    # typer.BadParameter maps to Click's usage-error exit code.
    assert result.exit_code == 2


def test_record_hosted(monkeypatch):
    monkeypatch.setattr(
        cli_main.httpx, "post", lambda *a, **kw: _FakeResponse({"records": 5})
    )
    result = runner.invoke(
        cli_main.app,
        [
            "record",
            "--commit", "abc123def456",
            "--file", "app/checkout.py",
            "--lines", "42",
            "--api-key", "ca_live_test",
        ],
    )
    assert result.exit_code == 0
    assert "(hosted, 5 total)" in result.stdout


def test_record_hosted_failure(monkeypatch):
    def fake_post(*a, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(cli_main.httpx, "post", fake_post)
    result = runner.invoke(
        cli_main.app,
        [
            "record",
            "--commit", "abc123def456",
            "--file", "app/checkout.py",
            "--lines", "42",
            "--api-key", "ca_live_test",
        ],
    )
    assert result.exit_code == 2
    assert "Failed to record to hosted service" in result.stdout

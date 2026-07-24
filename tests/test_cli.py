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

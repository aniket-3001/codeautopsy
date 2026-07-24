"""Tests for Prognosis: the pre-mortem PR bot.

Diff parsing is verified against real `git diff --unified=0` output (not a hand-rolled
fixture) since hunk-header line-number bookkeeping is exactly the kind of thing that looks
right and is off by one. The blame-based resolution and flag-pricing math run against a
real temporary git repo + a real ProvenanceStore, the same combination `test_provenance.py`
uses for the crash-side join.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from codeautopsy.prognosis.core import (
    PrognosisError,
    changed_lines,
    compute_flag_stats,
    post_comment,
    render_markdown,
    resolve_line,
    scan,
)
from codeautopsy.prognosis.models import FlagStats, LineFinding, PrognosisReport
from codeautopsy.provenance.models import IncidentRecord, ProvenanceRecord
from codeautopsy.provenance.store import ProvenanceStore


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
    _git(repo, "config", "user.email", "t@t.co")
    _git(repo, "config", "user.name", "t")
    return repo


def _record(commit: str, file_path: str, start: int, end: int, **kw) -> ProvenanceRecord:
    base = dict(
        commit_sha=commit,
        file_path=file_path,
        line_start=start,
        line_end=end,
        decision_span_id="e91ca75cd1ae81e4",
        decision_trace_id="c51641b768a8a67ea979f9005ade2f55",
        session_id="sess_test",
        reasoning_summary="assuming the input is always valid",
        risk_flags=["assumed_valid_input"],
        decision_id="dec_1",
    )
    base.update(kw)
    return ProvenanceRecord(**base)


# --- changed_lines: diff parsing -------------------------------------------------------------


def test_changed_lines_pure_addition(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "app.py").write_text("line1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    (repo / "app.py").write_text("line1\nline2\nline3\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "head")
    head = _git(repo, "rev-parse", "HEAD")

    result = changed_lines(repo, base, head)
    assert result == {"app.py": [(2, "line2"), (3, "line3")]}


def test_changed_lines_mixed_add_remove_tracks_shift(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "app.py").write_text("a\nb\nc\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    (repo / "app.py").write_text("a\nB2\nc\nd\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "head")
    head = _git(repo, "rev-parse", "HEAD")

    result = changed_lines(repo, base, head)
    # "b" -> "B2" is a replace (line 2), "d" is a pure addition (line 4).
    assert result == {"app.py": [(2, "B2"), (4, "d")]}


def test_changed_lines_new_file(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "app.py").write_text("a\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    (repo / "new.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "head")
    head = _git(repo, "rev-parse", "HEAD")

    result = changed_lines(repo, base, head)
    assert result == {"new.py": [(1, "x = 1"), (2, "y = 2")]}


def test_changed_lines_no_diff(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "app.py").write_text("a\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    assert changed_lines(repo, base, base) == {}


def test_changed_lines_raises_on_bad_repo(tmp_path: Path):
    import pytest

    with pytest.raises(PrognosisError):
        changed_lines(tmp_path, "base", "head")


# --- resolve_line: blame join against a PR's own commits ------------------------------------


def test_resolve_line_finds_decision_at_head(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")

    (repo / "app.py").write_text("x = 1\ny = int(code)\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "pr change")
    head = _git(repo, "rev-parse", "HEAD")

    store = ProvenanceStore(tmp_path / "p.db")
    store.add(_record(head, "app.py", 2, 2))

    rec = resolve_line(store, repo, "app.py", 2, head_ref=head)
    assert rec is not None
    assert rec.decision_id == "dec_1"


def test_resolve_line_returns_none_when_no_decision_indexed(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    head = _git(repo, "rev-parse", "HEAD")

    store = ProvenanceStore(tmp_path / "p.db")
    assert resolve_line(store, repo, "app.py", 1, head_ref=head) is None


# --- compute_flag_stats -----------------------------------------------------------------------


def test_compute_flag_stats_prices_crash_rate(tmp_path: Path):
    store = ProvenanceStore(tmp_path / "p.db")
    store.add(_record("c1", "a.py", 1, 1, decision_id="d1", risk_flags=["assumed_valid_input"]))
    store.add(_record("c2", "a.py", 2, 2, decision_id="d2", risk_flags=["assumed_valid_input"]))
    store.add(_record("c3", "a.py", 3, 3, decision_id="d3", risk_flags=["todo_left"]))

    store.add_incident(IncidentRecord(commit_sha="c1", file_path="a.py", line=1, decision_id="d1"))

    stats = compute_flag_stats(store)
    assert stats["assumed_valid_input"].decisions == 2
    assert stats["assumed_valid_input"].crashed_decisions == 1
    assert stats["assumed_valid_input"].crash_rate == 0.5
    assert stats["todo_left"].decisions == 1
    assert stats["todo_left"].crashed_decisions == 0
    assert stats["todo_left"].crash_rate == 0.0


def test_flag_stats_crash_rate_none_when_no_decisions():
    assert FlagStats(flag="x").crash_rate is None


# --- scan: end to end --------------------------------------------------------------------------


def test_scan_prices_a_risky_pr_line(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    (repo / "app.py").write_text("x = 1\ny = int(code)\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "pr change")
    head = _git(repo, "rev-parse", "HEAD")

    store = ProvenanceStore(tmp_path / "p.db")
    # Historical track record: 2 prior decisions carrying this flag, 1 crashed.
    store.add(_record("hist1", "other.py", 1, 1, decision_id="hist1", risk_flags=["assumed_valid_input"]))
    store.add(_record("hist2", "other.py", 2, 2, decision_id="hist2", risk_flags=["assumed_valid_input"]))
    store.add_incident(IncidentRecord(commit_sha="hist1", file_path="other.py", line=1, decision_id="hist1"))
    # The PR's own decision, carrying the same flag.
    store.add(_record(head, "app.py", 2, 2, decision_id="pr_dec", risk_flags=["assumed_valid_input"]))

    report = scan(store, repo, base, head, min_samples=2)
    assert report.lines_scanned == 1
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.file_path == "app.py"
    assert finding.line == 2
    assert finding.source == "decision"
    assert finding.decision_id == "pr_dec"
    # 3 decisions total carry this flag (2 historical + the PR's own), 1 crashed.
    assert finding.crash_rate == 1 / 3
    assert finding.worst_flag == "assumed_valid_input"


def test_scan_below_min_samples_leaves_crash_rate_unpriced(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    (repo / "app.py").write_text("x = 1\ny = int(code)\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "pr change")
    head = _git(repo, "rev-parse", "HEAD")

    store = ProvenanceStore(tmp_path / "p.db")
    store.add(_record(head, "app.py", 2, 2, decision_id="pr_dec", risk_flags=["assumed_valid_input"]))

    report = scan(store, repo, base, head, min_samples=2)
    assert len(report.findings) == 1
    assert report.findings[0].crash_rate is None


def test_scan_falls_back_to_pattern_scan_without_decision(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    (repo / "app.py").write_text("x = 1\n# TODO: handle this properly\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "pr change, no hook recorded")
    head = _git(repo, "rev-parse", "HEAD")

    store = ProvenanceStore(tmp_path / "p.db")  # empty — no decision indexed for this commit

    report = scan(store, repo, base, head)
    assert len(report.findings) == 1
    assert report.findings[0].source == "pattern"
    assert "todo_left" in report.findings[0].risk_flags
    assert report.findings[0].decision_id == ""


def test_scan_clean_diff_has_no_findings(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    (repo / "app.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "boring change")
    head = _git(repo, "rev-parse", "HEAD")

    store = ProvenanceStore(tmp_path / "p.db")
    report = scan(store, repo, base, head)
    assert report.lines_scanned == 1
    assert report.findings == []


def test_scan_sorts_priced_findings_by_crash_rate_desc(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "app.py").write_text("a\nb\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    (repo / "app.py").write_text("a\nb\nrisky_low\nrisky_high\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "pr change")
    head = _git(repo, "rev-parse", "HEAD")

    store = ProvenanceStore(tmp_path / "p.db")
    store.add(_record(head, "app.py", 3, 3, decision_id="low_dec", risk_flags=["skipped_tests"]))
    store.add(_record(head, "app.py", 4, 4, decision_id="high_dec", risk_flags=["hardcoded_value"]))
    # skipped_tests: 1/4 crash rate. hardcoded_value: 3/4 crash rate.
    for i in range(4):
        cid = f"hist_low_{i}"
        store.add(_record(f"h{i}", "x.py", i, i, decision_id=cid, risk_flags=["skipped_tests"]))
    store.add_incident(IncidentRecord(commit_sha="h0", file_path="x.py", line=0, decision_id="hist_low_0"))
    for i in range(4):
        cid = f"hist_high_{i}"
        store.add(_record(f"g{i}", "y.py", i, i, decision_id=cid, risk_flags=["hardcoded_value"]))
        store.add_incident(IncidentRecord(commit_sha=f"g{i}", file_path="y.py", line=i, decision_id=cid))
    store.add_incident(IncidentRecord(commit_sha="g_extra", file_path="y.py", line=0, decision_id="hist_high_0"))
    # (hardcoded_value ends up 4/4=1.0 crashed since every hist_high decision got an incident)

    report = scan(store, repo, base, head, min_samples=2)
    assert len(report.findings) == 2
    assert report.findings[0].worst_flag == "hardcoded_value"
    assert report.findings[1].worst_flag == "skipped_tests"
    assert report.findings[0].crash_rate > report.findings[1].crash_rate


# --- render_markdown ---------------------------------------------------------------------------


def test_render_markdown_clean_bill_of_health():
    report = PrognosisReport(base_ref="main", head_ref="HEAD", lines_scanned=5, findings=[])
    body = render_markdown(report)
    assert "Clean bill of health" in body


def test_render_markdown_includes_priced_and_unpriced_sections():
    report = PrognosisReport(
        base_ref="main",
        head_ref="HEAD",
        lines_scanned=2,
        findings=[
            LineFinding(
                file_path="app.py", line=10, risk_flags=["assumed_valid_input"],
                reasoning_summary="assumed valid", decision_id="d1", source="decision",
                crash_rate=0.5, worst_flag="assumed_valid_input", sample_size=4,
            ),
            LineFinding(
                file_path="app.py", line=20, risk_flags=["todo_left"], source="pattern",
            ),
        ],
    )
    body = render_markdown(report, min_samples=2)
    assert "app.py:10" in body
    assert "50%" in body
    assert "app.py:20" in body
    assert "fewer than 2 historical decisions" in body


# --- post_comment: graceful degradation, mirroring fixbot.open_pull_request ------------------


def test_post_comment_returns_none_when_gh_unavailable(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    import codeautopsy.prognosis.core as core_module

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("gh not found")

    monkeypatch.setattr(core_module.subprocess, "run", fake_run)
    assert post_comment(repo, "some body") is None


def test_post_comment_returns_none_when_gh_fails(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    import codeautopsy.prognosis.core as core_module

    monkeypatch.setattr(
        core_module.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="no PR"),
    )
    assert post_comment(repo, "some body") is None


def test_post_comment_returns_url_on_success(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    import codeautopsy.prognosis.core as core_module

    url = "https://github.com/example/repo/pull/1#issuecomment-1"
    monkeypatch.setattr(
        core_module.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, returncode=0, stdout=url + "\n", stderr=""),
    )
    assert post_comment(repo, "some body") == url


def test_post_comment_passes_pr_argument(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    import codeautopsy.prognosis.core as core_module

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(core_module.subprocess, "run", fake_run)
    post_comment(repo, "body", pr="42")
    assert "42" in captured["cmd"]

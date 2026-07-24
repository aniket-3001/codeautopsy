"""Tests for the Fix Bot: genealogy assembly, structured-output parsing, and the full
apply -> verify -> commit loop against a real (temporary) git repo.

The Groq call is always mocked — these tests prove the mechanism (git safety, the
verify-before-commit gate, branch hygiene), not the model's judgment.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codeautopsy.config import Settings
from codeautopsy.enricher.incidents import append_incident
from codeautopsy.fixbot.core import (
    FixBotError,
    build_genealogy,
    open_pull_request,
    propose_fix,
    run_fixbot,
)
from codeautopsy.fixbot.core import _git as _core_git
from codeautopsy.fixbot.models import FixProposal
from codeautopsy.provenance.models import ProvenanceRecord, ResolveResponse


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


BUGGY_SOURCE = """def parse_amount(code):
    return int(code)
"""

FIXED_SOURCE = """def parse_amount(code):
    if not code.isdigit():
        raise ValueError(f"not a valid amount: {code!r}")
    return int(code)
"""

REGRESSION_TEST = """import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app import parse_amount


def test_parse_amount_rejects_non_numeric():
    with pytest.raises(ValueError):
        parse_amount("GIMME50")
"""

BROKEN_REGRESSION_TEST = """def test_always_fails():
    assert False
"""


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
    _git(repo, "config", "user.email", "t@t.co")
    _git(repo, "config", "user.name", "t")
    (repo / "app.py").write_text(BUGGY_SOURCE, encoding="utf-8")
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "__init__.py").write_text("", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed bug")
    return repo


def _settings_for(repo: Path) -> Settings:
    return Settings(
        GROQ_API_KEY="test-key",
        CODEAUTOPSY_TARGET_REPO=str(repo),
        CODEAUTOPSY_PROVENANCE_DB=str(repo / "provenance.db"),
    )


# --- genealogy assembly ---------------------------------------------------------------------


def test_build_genealogy_combines_provenance_and_incident(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")

    rec = ProvenanceRecord(
        commit_sha=head,
        file_path="app.py",
        line_start=2,
        line_end=2,
        decision_span_id="e91ca75cd1ae81e4",
        decision_trace_id="c51641b768a8a67ea979f9005ade2f55",
        session_id="s1",
        reasoning_summary="assuming input is always valid",
        risk_flags=["assumed_valid_input"],
        decision_id="dec_1",
    )
    monkeypatch.setattr(
        "codeautopsy.fixbot.core.resolve_decision",
        lambda *a, **k: ResolveResponse(resolved=True, introducing_commit=head, record=rec),
    )
    append_incident(
        repo,
        {
            "file_path": "app.py",
            "line": 2,
            "exc_type": "ValueError",
            "exc_message": "invalid literal for int() with base 10: 'GIMME50'",
            "cause_of_death": "invalid value — unvalidated input",
            "resolved": True,
            "decision_id": "dec_1",
            "context": {"discount_code": "GIMME50"},
        },
    )

    genealogy = build_genealogy(_settings_for(repo), head, "app.py", 2)
    assert genealogy.reasoning_summary == "assuming input is always valid"
    assert genealogy.risk_flags == ["assumed_valid_input"]
    assert genealogy.exc_type == "ValueError"
    assert genealogy.context == {"discount_code": "GIMME50"}
    assert "def parse_amount" in genealogy.file_content


def test_build_genealogy_missing_file_raises(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(
        "codeautopsy.fixbot.core.resolve_decision",
        lambda *a, **k: ResolveResponse(resolved=False),
    )
    with pytest.raises(FixBotError):
        build_genealogy(_settings_for(repo), "deadbeef", "nope.py", 1)


# --- propose_fix (mocked model) --------------------------------------------------------------


def _sectioned_response(
    explanation: str, lesson: str, fixed_file_content: str, regression_test_code: str
) -> str:
    """Build a well-formed response in the plain-text ===SECTION=== protocol `_prompt` asks
    the model to follow (see codeautopsy/fixbot/core.py for why this replaced JSON tool-use).
    """
    return (
        "===EXPLANATION===\n"
        f"{explanation}\n"
        "===LESSON===\n"
        f"{lesson}\n"
        "===FIXED_FILE===\n"
        f"{fixed_file_content}\n"
        "===REGRESSION_TEST===\n"
        f"{regression_test_code}\n"
        "===END===\n"
    )


class _FakeMessage:
    def __init__(self, content: str):
        self.content = content


class _FakeChoice:
    def __init__(self, content: str):
        self.message = _FakeMessage(content)


class _FakeCompletion:
    def __init__(self, content: str):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, content: str):
        self._content = content

    def create(self, **kwargs):
        return _FakeCompletion(self._content)


class _FakeChat:
    def __init__(self, content: str):
        self.completions = _FakeCompletions(content)


class _FakeGroqClient:
    def __init__(self, content: str, **kwargs):
        self.chat = _FakeChat(content)


def _patch_groq(monkeypatch, proposal_input: dict):
    import groq

    content = _sectioned_response(**proposal_input)
    monkeypatch.setattr(groq, "Groq", lambda **kwargs: _FakeGroqClient(content))


def test_propose_fix_parses_tool_use_response(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    settings = _settings_for(repo)
    _patch_groq(
        monkeypatch,
        {
            "explanation": "validate before parsing",
            "fixed_file_content": FIXED_SOURCE,
            "regression_test_code": REGRESSION_TEST,
            "lesson": "always validate external input before int()",
        },
    )
    from codeautopsy.fixbot.models import Genealogy

    genealogy = Genealogy(file_path="app.py", line=2, commit_sha="x", file_content=BUGGY_SOURCE)
    proposal = propose_fix(genealogy, settings)
    assert isinstance(proposal, FixProposal)
    assert "validate" in proposal.explanation
    assert "def parse_amount" in proposal.fixed_file_content


class _SequencedCompletions:
    def __init__(self, responses: list):
        self._responses = list(responses)

    def create(self, **kwargs):
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, dict):
            item = _sectioned_response(**item)
        return _FakeCompletion(item)


class _SequencedChat:
    def __init__(self, responses: list):
        self.completions = _SequencedCompletions(responses)


class _SequencedGroqClient:
    def __init__(self, responses: list, **kwargs):
        self.chat = _SequencedChat(responses)


def _patch_groq_sequence(monkeypatch, responses: list):
    import groq

    monkeypatch.setattr(groq, "Groq", lambda **kwargs: _SequencedGroqClient(responses))


def test_propose_fix_strips_markdown_fences(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    settings = _settings_for(repo)
    _patch_groq(
        monkeypatch,
        {
            "explanation": "validate before parsing",
            "fixed_file_content": "```python\n" + FIXED_SOURCE + "```",
            "regression_test_code": "```\n" + REGRESSION_TEST + "```",
            "lesson": "always validate external input before int()",
        },
    )
    from codeautopsy.fixbot.models import Genealogy

    genealogy = Genealogy(file_path="app.py", line=2, commit_sha="x", file_content=BUGGY_SOURCE)
    proposal = propose_fix(genealogy, settings)
    assert not proposal.fixed_file_content.strip().startswith("```")
    assert not proposal.fixed_file_content.strip().endswith("```")
    assert "def parse_amount" in proposal.fixed_file_content
    assert not proposal.regression_test_code.strip().startswith("```")


def test_propose_fix_repairs_syntax_error_then_succeeds(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    settings = _settings_for(repo)
    broken = {
        "explanation": "attempted fix",
        "fixed_file_content": "def parse_amount(code:\n    return int(code)",  # invalid syntax
        "regression_test_code": REGRESSION_TEST,
        "lesson": "n/a",
    }
    fixed = {
        "explanation": "validate before parsing",
        "fixed_file_content": FIXED_SOURCE,
        "regression_test_code": REGRESSION_TEST,
        "lesson": "always validate external input before int()",
    }
    _patch_groq_sequence(monkeypatch, [broken, fixed])
    from codeautopsy.fixbot.models import Genealogy

    genealogy = Genealogy(file_path="app.py", line=2, commit_sha="x", file_content=BUGGY_SOURCE)
    proposal = propose_fix(genealogy, settings)
    assert "def parse_amount" in proposal.fixed_file_content
    assert "not code.isdigit()" in proposal.fixed_file_content


def test_propose_fix_raises_cleanly_after_repeated_syntax_errors(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    settings = _settings_for(repo)
    broken = {
        "explanation": "attempted fix",
        "fixed_file_content": "def parse_amount(code:\n    return int(code)",
        "regression_test_code": REGRESSION_TEST,
        "lesson": "n/a",
    }
    _patch_groq_sequence(monkeypatch, [broken, broken, broken])
    from codeautopsy.fixbot.models import Genealogy

    genealogy = Genealogy(file_path="app.py", line=2, commit_sha="x", file_content=BUGGY_SOURCE)
    with pytest.raises(FixBotError, match="invalid Python"):
        propose_fix(genealogy, settings)


def test_propose_fix_converts_groq_error_to_fixboterror(tmp_path: Path, monkeypatch):
    import groq

    repo = _init_repo(tmp_path)
    settings = _settings_for(repo)
    _patch_groq_sequence(
        monkeypatch,
        [groq.GroqError("400 tool_use_failed")] * 3,
    )
    from codeautopsy.fixbot.models import Genealogy

    genealogy = Genealogy(file_path="app.py", line=2, commit_sha="x", file_content=BUGGY_SOURCE)
    with pytest.raises(FixBotError, match="model call failed"):
        propose_fix(genealogy, settings)


def test_propose_fix_recovers_from_one_groq_error(tmp_path: Path, monkeypatch):
    import groq

    repo = _init_repo(tmp_path)
    settings = _settings_for(repo)
    fixed = {
        "explanation": "validate before parsing",
        "fixed_file_content": FIXED_SOURCE,
        "regression_test_code": REGRESSION_TEST,
        "lesson": "always validate external input before int()",
    }
    _patch_groq_sequence(monkeypatch, [groq.GroqError("400 tool_use_failed"), fixed])
    from codeautopsy.fixbot.models import Genealogy

    genealogy = Genealogy(file_path="app.py", line=2, commit_sha="x", file_content=BUGGY_SOURCE)
    proposal = propose_fix(genealogy, settings)
    assert "def parse_amount" in proposal.fixed_file_content


def test_propose_fix_requires_api_key(tmp_path: Path):
    repo = _init_repo(tmp_path)
    settings = Settings(
        GROQ_API_KEY=None,
        CODEAUTOPSY_TARGET_REPO=str(repo),
        CODEAUTOPSY_PROVENANCE_DB=str(repo / "provenance.db"),
    )
    from codeautopsy.fixbot.models import Genealogy

    genealogy = Genealogy(file_path="app.py", line=2, commit_sha="x", file_content=BUGGY_SOURCE)
    with pytest.raises(FixBotError):
        propose_fix(genealogy, settings)


# --- run_fixbot: the full apply -> verify -> commit loop, against a real repo ----------------


def _stub_run(monkeypatch, repo: Path, head: str, proposal_input: dict):
    rec = ProvenanceRecord(
        commit_sha=head,
        file_path="app.py",
        line_start=2,
        line_end=2,
        decision_span_id="e91ca75cd1ae81e4",
        decision_trace_id="c51641b768a8a67ea979f9005ade2f55",
        session_id="s1",
        reasoning_summary="assuming input is always valid",
        risk_flags=["assumed_valid_input"],
        decision_id="dec_1",
    )
    monkeypatch.setattr(
        "codeautopsy.fixbot.core.resolve_decision",
        lambda *a, **k: ResolveResponse(resolved=True, introducing_commit=head, record=rec),
    )
    _patch_groq(monkeypatch, proposal_input)


def test_run_fixbot_verified_fix_commits_and_restores_branch(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    original_branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    _stub_run(
        monkeypatch,
        repo,
        head,
        {
            "explanation": "validate before parsing",
            "fixed_file_content": FIXED_SOURCE,
            "regression_test_code": REGRESSION_TEST,
            "lesson": "always validate external input before int()",
        },
    )

    result = run_fixbot(_settings_for(repo), head, "app.py", 2, push=False)

    assert result.verified is True
    assert result.branch == "codeautopsy/fix-dec_1"
    assert result.commit_sha is not None
    assert result.pr_url is None  # push=False

    # Working tree must be restored to the original branch, untouched.
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == original_branch
    assert _git(repo, "status", "--porcelain") == ""
    assert (repo / "app.py").read_text(
        encoding="utf-8"
    ) == BUGGY_SOURCE  # unchanged on original branch

    # But the fix branch really has the patch + a passing regression test.
    fixed_on_branch = subprocess.run(
        ["git", "-C", str(repo), "show", f"{result.branch}:app.py"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "isdigit" in fixed_on_branch


def test_run_fixbot_failed_verification_leaves_no_trace(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    original_branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    _stub_run(
        monkeypatch,
        repo,
        head,
        {
            "explanation": "attempted fix",
            "fixed_file_content": BUGGY_SOURCE,  # doesn't actually fix anything
            "regression_test_code": BROKEN_REGRESSION_TEST,
            "lesson": "n/a",
        },
    )

    result = run_fixbot(_settings_for(repo), head, "app.py", 2, push=False)

    assert result.verified is False
    assert "regression test failed" in result.detail
    assert result.branch is None
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == original_branch
    assert _git(repo, "status", "--porcelain") == ""  # cleaned up, nothing left dangling
    branches = _git(repo, "branch", "--list")
    assert "codeautopsy/fix-dec_1" not in branches


def test_run_fixbot_refuses_dirty_worktree(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    (repo / "scratch.txt").write_text("uncommitted work in progress", encoding="utf-8")

    result = run_fixbot(_settings_for(repo), head, "app.py", 2, push=False)

    assert result.verified is False
    assert "not clean" in result.detail
    assert (repo / "scratch.txt").exists()  # untouched, not deleted


# --- _git error path, propose_fix mismatch, open_pull_request -------------------------------


def test_git_helper_raises_fixboterror_on_failure(tmp_path: Path):
    with pytest.raises(FixBotError):
        _core_git(tmp_path, "not-a-real-git-command")


def test_propose_fix_raises_cleanly_when_response_missing_section_markers(
    tmp_path: Path, monkeypatch
):
    repo = _init_repo(tmp_path)
    settings = _settings_for(repo)
    # Plain prose with no ===SECTION=== markers at all.
    _patch_groq_sequence(
        monkeypatch,
        ["Sure, here's a fix for your bug: just add a check before splitting."] * 3,
    )
    from codeautopsy.fixbot.models import Genealogy

    genealogy = Genealogy(file_path="app.py", line=2, commit_sha="x", file_content=BUGGY_SOURCE)
    with pytest.raises(FixBotError, match="required ===SECTION=== format"):
        propose_fix(genealogy, settings)


def test_propose_fix_repairs_missing_markers_then_succeeds(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    settings = _settings_for(repo)
    malformed = "Sure, here's a fix — but I forgot the markers entirely."
    fixed = {
        "explanation": "validate before parsing",
        "fixed_file_content": FIXED_SOURCE,
        "regression_test_code": REGRESSION_TEST,
        "lesson": "always validate external input before int()",
    }
    _patch_groq_sequence(monkeypatch, [malformed, fixed])
    from codeautopsy.fixbot.models import Genealogy

    genealogy = Genealogy(file_path="app.py", line=2, commit_sha="x", file_content=BUGGY_SOURCE)
    proposal = propose_fix(genealogy, settings)
    assert "def parse_amount" in proposal.fixed_file_content


def test_open_pull_request_returns_none_without_remote(tmp_path: Path):
    repo = _init_repo(tmp_path)
    assert open_pull_request(repo, "some-branch", title="t", body="b") is None


def test_open_pull_request_returns_none_when_gh_unavailable(tmp_path: Path, monkeypatch):
    """Remote is configured and the push succeeds, but `gh` isn't installed/authenticated —
    open_pull_request must degrade to None rather than raising.
    """
    repo = _init_repo(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True, check=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "checkout", "-b", "fix-branch")
    (repo / "app.py").write_text(FIXED_SOURCE, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "fix")

    import codeautopsy.fixbot.core as core_module

    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if cmd[0] == "gh":
            return subprocess.CompletedProcess(
                cmd, returncode=1, stdout="", stderr="gh: not authenticated"
            )
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(core_module.subprocess, "run", fake_run)

    result = open_pull_request(repo, "fix-branch", title="t", body="b")

    assert result is None


def test_open_pull_request_returns_none_when_push_fails(tmp_path: Path):
    """Remote is configured but unreachable — `git push` itself fails (raises FixBotError
    from the _git helper), which open_pull_request must swallow and degrade to None.
    """
    repo = _init_repo(tmp_path)
    _git(repo, "remote", "add", "origin", str(tmp_path / "does-not-exist.git"))
    _git(repo, "checkout", "-b", "fix-branch")

    result = open_pull_request(repo, "fix-branch", title="t", body="b")

    assert result is None


def test_open_pull_request_returns_pr_url_on_success(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True, check=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "checkout", "-b", "fix-branch")
    (repo / "app.py").write_text(FIXED_SOURCE, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "fix")

    import codeautopsy.fixbot.core as core_module

    real_run = subprocess.run
    pr_url = "https://github.com/example/repo/pull/1"

    def fake_run(cmd, **kwargs):
        if cmd[0] == "gh":
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=pr_url + "\n", stderr="")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(core_module.subprocess, "run", fake_run)

    result = open_pull_request(repo, "fix-branch", title="t", body="b")

    assert result == pr_url


def test_run_fixbot_push_true_without_remote_stays_committed_locally(tmp_path: Path, monkeypatch):
    """push=True still succeeds locally when there's no remote configured — open_pull_request
    degrades to None rather than raising, and the fix is left committed either way.
    """
    repo = _init_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    _stub_run(
        monkeypatch,
        repo,
        head,
        {
            "explanation": "validate before parsing",
            "fixed_file_content": FIXED_SOURCE,
            "regression_test_code": REGRESSION_TEST,
            "lesson": "always validate external input before int()",
        },
    )

    result = run_fixbot(_settings_for(repo), head, "app.py", 2, push=True)

    assert result.verified is True
    assert result.pr_url is None

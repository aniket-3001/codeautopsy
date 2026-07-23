"""Fix Bot — the loop-closer.

`resolve` told us *why* a line was written (the agent's own reasoning) and *what* actually
broke it (the incident log). The Fix Bot feeds both back to the agent, verified as a *tool
call* (not parsed prose) so the response is always well-formed, applies the patch, proves it
with a real regression test derived from the exact failing input, and — only if that test
actually passes — commits it on its own branch and opens a PR carrying the chain of custody
as evidence.

Model calls go through Groq (OpenAI-compatible chat-completions API, free tier) rather than a
paid provider — forced tool-choice gives the same structured, no-prose-parsing guarantee.

Safety: this mutates a git working tree, so every step that touches it refuses to run unless
the tree is clean beforehand, and failed verification never leaves a commit behind.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from codeautopsy.config import Settings, get_settings
from codeautopsy.enricher.core import resolve_decision
from codeautopsy.enricher.incidents import latest_incident_for
from codeautopsy.fixbot.models import FixBotResult, FixProposal, Genealogy

FIX_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_fix",
        "description": "Submit the corrected file content and a regression test proving the fix.",
        "parameters": {
            "type": "object",
            "properties": {
                "explanation": {
                    "type": "string",
                    "description": (
                        "One short paragraph: what was wrong and why this fix is correct."
                    ),
                },
                "fixed_file_content": {
                    "type": "string",
                    "description": "The FULL corrected content of the source file, ready to write.",
                },
                "regression_test_code": {
                    "type": "string",
                    "description": (
                        "A complete, standalone pytest test function (its own `def test_...():` "
                        "block, including any needed imports at the top) that reproduces the "
                        "exact original crash via the app's real entrypoint and asserts it no "
                        "longer raises / now behaves correctly."
                    ),
                },
                "lesson": {
                    "type": "string",
                    "description": (
                        "One sentence generalizing the mistake into a rule ('always validate "
                        "external input before int()'), to feed back into the agent's own rules."
                    ),
                },
            },
            "required": ["explanation", "fixed_file_content", "regression_test_code", "lesson"],
        },
    },
}


class FixBotError(RuntimeError):
    pass


def _git(repo: str | Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise FixBotError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout.strip()


def build_genealogy(
    settings: Settings, commit_sha: str, file_path: str, line: int
) -> Genealogy:
    """Assemble everything the model needs: why the line exists, and what broke it."""
    resolution = resolve_decision(settings, commit_sha, file_path, line)
    repo_root = settings.target_repo
    full_path = repo_root / file_path
    if not full_path.exists():
        raise FixBotError(f"{full_path} does not exist under target repo {repo_root}")
    file_content = full_path.read_text(encoding="utf-8")

    incident = latest_incident_for(repo_root, file_path, line)
    rec = resolution.record

    return Genealogy(
        file_path=file_path,
        line=line,
        commit_sha=commit_sha,
        file_content=file_content,
        reasoning_summary=rec.reasoning_summary if rec else "",
        risk_flags=rec.risk_flags if rec else [],
        decision_id=rec.decision_id if rec else "",
        exc_type=(incident or {}).get("exc_type", ""),
        exc_message=(incident or {}).get("exc_message", ""),
        cause_of_death=(incident or {}).get("cause_of_death", ""),
        context=(incident or {}).get("context", {}),
    )


def _prompt(genealogy: Genealogy) -> str:
    return f"""You are patching your own mistake.

You previously wrote {genealogy.file_path} around line {genealogy.line}, reasoning at the
time: "{genealogy.reasoning_summary or '(no reasoning captured)'}"
Risk flags raised at write-time: {", ".join(genealogy.risk_flags) or "none"}

That code just crashed in production:
  {genealogy.exc_type}: {genealogy.exc_message}
  cause of death: {genealogy.cause_of_death}
  triggering input: {genealogy.context}

Current full content of {genealogy.file_path}:
--- BEGIN FILE ---
{genealogy.file_content}
--- END FILE ---

Fix the bug with minimal, targeted changes — do not rewrite unrelated code. Then write one
standalone pytest regression test that reproduces the exact original crash (using the
triggering input above) against the app's real entrypoint (e.g. FastAPI TestClient importing
`codeautopsy.sample_app.main`) and asserts the fixed behavior. Call `submit_fix` with the
result."""


def propose_fix(genealogy: Genealogy, settings: Settings | None = None) -> FixProposal:
    """Ask the model to patch its own mistake. Uses forced tool-use so the response is
    always structured — no prose-parsing, no ambiguity about what to apply.
    """
    settings = settings or get_settings()
    if not settings.groq_api_key:
        raise FixBotError(
            "GROQ_API_KEY not set — required for the Fix Bot to call the model."
        )

    import groq

    client = groq.Groq(api_key=settings.groq_api_key)
    response = client.chat.completions.create(  # type: ignore[call-overload]
        model=settings.fixbot_model,
        max_tokens=4096,
        tools=[FIX_TOOL],
        tool_choice={"type": "function", "function": {"name": "submit_fix"}},
        messages=[{"role": "user", "content": _prompt(genealogy)}],
    )

    tool_calls = response.choices[0].message.tool_calls or []
    for call in tool_calls:
        if call.function.name == "submit_fix":
            return FixProposal(**json.loads(call.function.arguments))
    raise FixBotError("model did not return a submit_fix tool call")


def apply_fix(
    repo_root: Path, file_path: str, test_file_rel: str, proposal: FixProposal
) -> None:
    """Write the patched source and the regression test to the working tree."""
    (repo_root / file_path).write_text(proposal.fixed_file_content, encoding="utf-8")
    test_path = repo_root / test_file_rel
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text(proposal.regression_test_code.strip() + "\n", encoding="utf-8")


def run_regression_test(repo_root: Path, test_file_rel: str) -> tuple[bool, str]:
    """Run just the new regression test. Returns (passed, captured output tail)."""
    proc = subprocess.run(
        ["python", "-m", "pytest", test_file_rel, "-q"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    output = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, output[-4000:]


def _worktree_is_clean(repo_root: Path) -> bool:
    return _git(repo_root, "status", "--porcelain") == ""


def commit_fix(
    repo_root: Path,
    branch_name: str,
    commit_message: str,
    paths: list[str],
) -> str:
    """Commit the already-written, already-verified fix on a new branch, then switch back
    to whatever branch was checked out before — this must never leave the caller's working
    directory on a different branch than it found it on.
    """
    original_branch = _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    _git(repo_root, "checkout", "-b", branch_name)
    try:
        _git(repo_root, "add", *paths)
        _git(repo_root, "commit", "-m", commit_message)
        return _git(repo_root, "rev-parse", "HEAD")
    finally:
        _git(repo_root, "checkout", original_branch)


def open_pull_request(
    repo_root: Path, branch_name: str, title: str, body: str, base: str = "master"
) -> str | None:
    """Push the fix branch and open a PR via `gh`. Returns the PR URL, or None if there's
    no remote / gh isn't authenticated — the fix stays committed locally either way.
    """
    remotes = _git(repo_root, "remote")
    if not remotes:
        return None
    try:
        _git(repo_root, "push", "-u", "origin", branch_name)
        proc = subprocess.run(
            ["gh", "pr", "create", "--title", title, "--body", body, "--base", base,
             "--head", branch_name],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else None
    except (FixBotError, FileNotFoundError):
        return None


def run_fixbot(
    settings: Settings,
    commit_sha: str,
    file_path: str,
    line: int,
    push: bool = False,
) -> FixBotResult:
    """The full loop: genealogy -> proposal -> apply -> verify -> (commit + PR)."""
    repo_root = settings.target_repo

    if not _worktree_is_clean(repo_root):
        return FixBotResult(
            verified=False,
            detail="working tree is not clean — refusing to touch it. Commit or stash first.",
        )

    genealogy = build_genealogy(settings, commit_sha, file_path, line)
    proposal = propose_fix(genealogy, settings)

    decision_tag = genealogy.decision_id or "unknown"
    test_file_rel = f"tests/test_fix_{decision_tag}.py"

    apply_fix(repo_root, file_path, test_file_rel, proposal)
    passed, output = run_regression_test(repo_root, test_file_rel)

    if not passed:
        # Leave no trace of a fix that doesn't work.
        _git(repo_root, "checkout", "--", file_path)
        (repo_root / test_file_rel).unlink(missing_ok=True)
        return FixBotResult(
            verified=False,
            explanation=proposal.explanation,
            test_output=output,
            detail="regression test failed against the proposed fix — nothing committed.",
        )

    branch = f"codeautopsy/fix-{decision_tag}"
    message = (
        f"Fix Bot: patch {decision_tag}\n\n"
        f"{proposal.explanation}\n\n"
        f"Cause of death: {genealogy.cause_of_death}\n"
        f"Original reasoning: \"{genealogy.reasoning_summary}\"\n"
        f"Risk flags at write-time: {', '.join(genealogy.risk_flags) or 'none'}\n"
        f"Lesson: {proposal.lesson}\n\n"
        f"Closes the loop from `codeautopsy autopsy {commit_sha} {file_path} {line}`."
    )
    commit_sha_new = commit_fix(repo_root, branch, message, [file_path, test_file_rel])

    pr_url = None
    if push:
        pr_url = open_pull_request(
            repo_root,
            branch,
            title=f"Fix Bot: patch {decision_tag}",
            body=message,
        )

    return FixBotResult(
        verified=True,
        explanation=proposal.explanation,
        lesson=proposal.lesson,
        test_output=output,
        branch=branch,
        commit_sha=commit_sha_new,
        pr_url=pr_url,
        detail="fix verified by regression test and committed",
    )

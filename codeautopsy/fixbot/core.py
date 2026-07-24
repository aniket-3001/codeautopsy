"""Fix Bot — the loop-closer.

`resolve` told us *why* a line was written (the agent's own reasoning) and *what* actually
broke it (the incident log). The Fix Bot feeds both back to the agent, parses its response
against a fixed plain-text section format (not JSON — see the note on `propose_fix` for why),
applies the patch, proves it with a real regression test derived from the exact failing input,
and — only if that test actually passes — commits it on its own branch and opens a PR carrying
the chain of custody as evidence.

Model calls go through Groq (OpenAI-compatible chat-completions API, free tier) rather than a
paid provider.

Safety: this mutates a git working tree, so every step that touches it refuses to run unless
the tree is clean beforehand, and failed verification never leaves a commit behind.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

from codeautopsy.config import Settings, get_settings
from codeautopsy.enricher.core import resolve_decision
from codeautopsy.enricher.incidents import latest_incident_for
from codeautopsy.fixbot.models import FixBotResult, FixProposal, Genealogy


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
triggering input above) by importing the actual module at `{genealogy.file_path}` — derive the
import from that file's real location in this repo, not from any other project's layout you may
be familiar with.

Respond in PLAIN TEXT using EXACTLY this format — no JSON, no markdown code fences, nothing
before or after. Each marker below must appear alone on its own line, exactly as written:

===EXPLANATION===
<one short paragraph: what was wrong and why this fix is correct>
===LESSON===
<one sentence generalizing the mistake into a rule, e.g. "always validate external input
before int()", to feed back into your own rules>
===FIXED_FILE===
<the FULL corrected content of {genealogy.file_path}, raw source only>
===REGRESSION_TEST===
<a complete, standalone pytest test — its own `def test_...():` block plus any needed
imports at the top, raw source only>
===END===
"""


def _strip_code_fence(text: str) -> str:
    """Model output is untrusted: it's told not to use markdown fences but sometimes does
    anyway. Strip a leading/trailing ``` fence (with optional language tag) if present.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


def _python_syntax_error(code: str) -> str | None:
    """Return a human-readable syntax error message, or None if `code` parses cleanly."""
    try:
        ast.parse(code)
    except SyntaxError as exc:
        return f"{exc.msg} at line {exc.lineno}"
    return None


_MAX_ATTEMPTS = 3


_SECTION_PATTERN = re.compile(
    r"===EXPLANATION===\s*(?P<explanation>.*?)\s*"
    r"===LESSON===\s*(?P<lesson>.*?)\s*"
    r"===FIXED_FILE===\s*(?P<fixed_file_content>.*?)\s*"
    r"===REGRESSION_TEST===\s*(?P<regression_test_code>.*?)\s*"
    r"===END===",
    re.DOTALL,
)


def _parse_sectioned_response(text: str) -> dict[str, str] | None:
    """Parse the plain-text `===SECTION===` protocol `_prompt` asks the model to follow.

    Deliberately not JSON: earlier attempts used forced tool-calling, but a model
    JSON-encoding multi-line source that itself contains quotes reliably produces
    malformed JSON (an unescaped or missing quote) that Groq's own parser rejects
    before the response ever reaches us. Plain delimited text has no escaping to get
    wrong, which removes that whole failure class rather than papering over it.
    """
    match = _SECTION_PATTERN.search(text)
    if not match:
        return None
    return match.groupdict()


def propose_fix(genealogy: Genealogy, settings: Settings | None = None) -> FixProposal:
    """Ask the model to patch its own mistake, parsing its response against the fixed
    plain-text section format described in `_prompt`.

    Even with an exact format to follow, the model can still ignore it (missing a
    marker), wrap code in markdown fences despite instructions not to, or emit
    syntactically invalid Python. Each of those gets one repair attempt (re-prompted
    with the specific problem) before giving up with a clean FixBotError — never a raw
    groq.GroqError or SyntaxError reaching the CLI user.
    """
    settings = settings or get_settings()
    if not settings.groq_api_key:
        raise FixBotError(
            "GROQ_API_KEY not set — required for the Fix Bot to call the model."
        )

    import groq

    client = groq.Groq(api_key=settings.groq_api_key)
    messages: list[dict] = [{"role": "user", "content": _prompt(genealogy)}]
    last_error = ""

    for attempt in range(_MAX_ATTEMPTS):
        final_attempt = attempt + 1 >= _MAX_ATTEMPTS
        try:
            response = client.chat.completions.create(
                model=settings.fixbot_model,
                max_tokens=4096,
                messages=messages,  # type: ignore[arg-type]
            )
        except groq.GroqError as exc:
            last_error = str(exc)
            if final_attempt:
                raise FixBotError(f"model call failed: {last_error}") from exc
            messages.append(
                {
                    "role": "user",
                    "content": f"Your previous request failed: {last_error}\nTry again.",
                }
            )
            continue

        content = response.choices[0].message.content or ""
        proposal_kwargs = _parse_sectioned_response(content)

        if proposal_kwargs is None:
            last_error = "response did not follow the required ===SECTION=== format"
            if final_attempt:
                raise FixBotError(last_error)
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Your response did not follow the required format. Reply again "
                        "using exactly the ===EXPLANATION===/===LESSON===/===FIXED_FILE===/"
                        "===REGRESSION_TEST===/===END=== markers, each alone on its own "
                        "line, with no JSON and no markdown fences."
                    ),
                }
            )
            continue

        proposal_kwargs["fixed_file_content"] = _strip_code_fence(
            proposal_kwargs["fixed_file_content"]
        )
        proposal_kwargs["regression_test_code"] = _strip_code_fence(
            proposal_kwargs["regression_test_code"]
        )

        syntax_errors = {
            field: _python_syntax_error(proposal_kwargs[field])
            for field in ("fixed_file_content", "regression_test_code")
        }
        syntax_errors = {k: v for k, v in syntax_errors.items() if v}
        if syntax_errors:
            last_error = "; ".join(f"{k}: {v}" for k, v in syntax_errors.items())
            if final_attempt:
                raise FixBotError(f"model produced invalid Python: {last_error}")
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Your submitted code has a syntax error: {last_error}\n"
                        "Reply again with corrected, syntactically valid Python in both "
                        "sections, using the same === marker format."
                    ),
                }
            )
            continue

        return FixProposal(**proposal_kwargs)

    raise FixBotError(f"model failed to produce a usable fix: {last_error}")


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

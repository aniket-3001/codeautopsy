"""CodeAutopsy CLI — the Coroner.

`codeautopsy autopsy <commit> <file> <line>` prints the chain of custody as a coroner's
report: which AI decision authored the crashing line, its reasoning, and its risk flags.
"""

from __future__ import annotations

import hashlib
import secrets
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from codeautopsy.config import get_settings
from codeautopsy.fixbot.core import FixBotError, run_fixbot
from codeautopsy.fixbot.models import FixBotResult
from codeautopsy.otel import force_utf8_stdout
from codeautopsy.prognosis.core import PrognosisError, post_comment, render_markdown, scan
from codeautopsy.provenance.models import ProvenanceRecord
from codeautopsy.provenance.store import ProvenanceStore
from codeautopsy.recorder.commit_indexer import index_pending_at_head

# Default hosted provenance API — where `codeautopsy record --api-key` posts decisions.
HOSTED_API_URL = "https://codeautopsy-provenance-3bczbiamba-uc.a.run.app"

force_utf8_stdout()
app = typer.Typer(help="CodeAutopsy — git blame for the AI era.", no_args_is_help=True)
console = Console()


def _parse_lines(lines: str) -> tuple[int, int]:
    """Parse a `--lines` value: `42` -> (42, 42); `40-46` -> (40, 46)."""
    text = lines.strip()
    if "-" in text:
        start_s, end_s = text.split("-", 1)
        start, end = int(start_s), int(end_s)
    else:
        start = end = int(text)
    if start > end:
        start, end = end, start
    return start, end


@app.command()
def autopsy(
    commit: str = typer.Argument(..., help="The deployed commit SHA (deployment.commit.sha)."),
    file: str = typer.Argument(..., help="File path, repo-relative (e.g. app/payment.py)."),
    line: int = typer.Argument(..., help="The crashing line number."),
) -> None:
    """Resolve a runtime crash (commit:file:line) to the AI decision that caused it."""
    settings = get_settings()
    try:
        resp = httpx.post(
            f"{settings.provenance_url}/resolve",
            json={"commit_sha": commit, "file_path": file, "line": line},
            timeout=5.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        console.print(f"[red]Provenance service unreachable:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    data = resp.json()
    if not data.get("resolved"):
        console.print(f"[yellow]Not resolved:[/yellow] {data.get('detail')}")
        raise typer.Exit(code=1)

    rec = data["record"]
    risk = ", ".join(rec["risk_flags"]) if rec["risk_flags"] else "none"
    body = (
        f"[bold]\"{rec['reasoning_summary']}\"[/bold]\n\n"
        f"file:            {rec['file_path']}:{rec['line_start']}-{rec['line_end']}\n"
        f"introducing commit: {data['introducing_commit'][:12]}\n"
        f"risk flags:      {risk}\n"
        f"decision id:     {rec['decision_id']}\n"
        f"session:         {rec['session_id']}\n"
        f"dev-time trace:  {rec['decision_trace_id']}\n"
        f"dev-time span:   {rec['decision_span_id']}"
    )
    console.print(Panel.fit(body, title="Coroner's report", border_style="red"))


@app.command()
def fix(
    commit: str = typer.Argument(..., help="The deployed commit SHA."),
    file: str = typer.Argument(..., help="File path, repo-relative."),
    line: int = typer.Argument(..., help="The crashing line number."),
    push: bool = typer.Option(
        False, "--push", help="Push the fix branch and open a PR via `gh` (needs a remote)."
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit the result as one JSON line (for the Auto-Heal workflow)."
    ),
) -> None:
    """Feed the agent its own autopsy: patch the bug, prove it with a regression test, commit."""
    settings = get_settings()
    if not as_json:
        console.print(
            "[bold]Fix Bot:[/bold] resolving genealogy and asking the agent to patch itself..."
        )
    try:
        result = run_fixbot(settings, commit, file, line, push=push)
    except FixBotError as exc:
        if as_json:
            # Machine-readable failure so the workflow can still report back cleanly.
            print(FixBotResult(verified=False, detail=str(exc)).model_dump_json())
            raise typer.Exit(code=2) from exc
        console.print(f"[red]Fix Bot failed:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    if as_json:
        # One JSON line on stdout — the workflow parses this to get pr_url/status.
        print(result.model_dump_json())
        raise typer.Exit(code=0 if result.verified else 1)

    if not result.verified:
        console.print(Panel.fit(
            f"[yellow]{result.detail}[/yellow]\n\n{result.test_output}",
            title="Fix Bot — verification failed", border_style="yellow",
        ))
        raise typer.Exit(code=1)

    body = (
        f"[bold]{result.explanation}[/bold]\n\n"
        f"lesson:     {result.lesson}\n"
        f"branch:     {result.branch}\n"
        f"commit:     {result.commit_sha[:12] if result.commit_sha else '-'}\n"
        f"pr:         {result.pr_url or '(not pushed — pass --push, or no remote configured)'}"
    )
    console.print(Panel.fit(body, title="Fix Bot — verified & committed", border_style="green"))


@app.command()
def prognose(
    base: str = typer.Argument(..., help="Base ref to diff against, e.g. origin/master."),
    head: str = typer.Option("HEAD", "--head", help="Head ref; defaults to the current checkout."),
    repo: Path = typer.Option(None, "--repo", help="Repo root; defaults to config target_repo."),
    min_samples: int = typer.Option(
        2, "--min-samples",
        help="Minimum historical decisions before trusting a flag's crash rate.",
    ),
    comment: bool = typer.Option(
        False, "--comment", help="Post the report as a PR comment via `gh` (needs a remote)."
    ),
    pr: str = typer.Option(
        None, "--pr", help="PR number/URL to comment on; defaults to the current branch's PR."
    ),
    fail_on_risk: bool = typer.Option(
        False, "--fail-on-risk",
        help="Exit non-zero if any line prices a nonzero historical crash rate (for CI gating).",
    ),
) -> None:
    """Prognosis Bot: scan a PR's diff for risky AI-authored lines before merge, priced
    against the crash history every risk flag has already built up in production."""
    settings = get_settings()
    repo_root = repo or settings.target_repo
    store = ProvenanceStore(settings.provenance_db)

    try:
        report = scan(store, repo_root, base, head, min_samples=min_samples)
    except PrognosisError as exc:
        console.print(f"[red]Prognosis failed:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    body = render_markdown(report, min_samples=min_samples)
    console.print(Panel.fit(body, title="Prognosis report", border_style="magenta"))

    if comment:
        url = post_comment(repo_root, body, pr=pr)
        if url:
            console.print(f"[green]Posted to PR:[/green] {url}")
        else:
            console.print(
                "[yellow]Could not post to PR (no remote, gh not authenticated, "
                "or no open PR).[/yellow]"
            )

    if fail_on_risk and any(f.crash_rate for f in report.findings):
        raise typer.Exit(code=1)


@app.command("index-commit")
def index_commit(
    repo: Path = typer.Option(None, "--repo", help="Repo root; defaults to config target_repo."),
) -> None:
    """Bind pending edit-time decisions to the just-made commit (run after `git commit`)."""
    settings = get_settings()
    repo_root = repo or settings.target_repo
    store = ProvenanceStore(settings.provenance_db)
    n = index_pending_at_head(repo_root, store)
    console.print(f"[green]Indexed {n} decision(s) into the provenance store.[/green]")


@app.command()
def record(
    commit: str = typer.Option(..., "--commit", help="Commit SHA the decision was made in."),
    file: str = typer.Option(..., "--file", help="File path, repo-relative (e.g. app/checkout.py)."),  # noqa: E501
    lines: str = typer.Option(..., "--lines", help="Line range the decision authored, e.g. 40-46."),
    reasoning: str = typer.Option("", "--reasoning", help="Why the code was written this way."),
    risk_flag: list[str] = typer.Option(
        None, "--risk-flag", help="A risk flag for the decision (repeatable)."
    ),
    tool: str = typer.Option("manual", "--tool", help="Which agent/tool made the decision."),
    model: str = typer.Option("", "--model", help="Model that authored it, if any."),
    api_key: str = typer.Option(
        None, "--api-key", envvar="CODEAUTOPSY_API_KEY",
        help="Hosted org API key. If set, records to the hosted service; else writes locally.",
    ),
    api_url: str = typer.Option(
        None, "--api-url", envvar="CODEAUTOPSY_API_URL",
        help=f"Hosted provenance API base (default {HOSTED_API_URL}).",
    ),
) -> None:
    """Record a decision from ANY agent — not just Claude Code.

    The Claude Code hook records automatically; this is the agent-agnostic path so any tool,
    script, or human can log the reasoning behind a line range and have crashes trace back to it.
    """
    line_start, line_end = _parse_lines(lines)
    # Content-anchored id: survives reformatting/rebase where line numbers drift.
    decision_id = "dec_" + hashlib.sha1(
        f"{file}:{line_start}-{line_end}:{reasoning}".encode()
    ).hexdigest()[:12]
    record = ProvenanceRecord(
        commit_sha=commit,
        file_path=file,
        line_start=line_start,
        line_end=line_end,
        decision_span_id=secrets.token_hex(8),
        decision_trace_id=secrets.token_hex(16),
        session_id=f"sess_{secrets.token_hex(4)}",
        reasoning_summary=reasoning,
        risk_flags=list(risk_flag or []),
        model=model,
        tool=tool,
        decision_id=decision_id,
    )

    if api_key:
        base = (api_url or HOSTED_API_URL).rstrip("/")
        try:
            resp = httpx.post(
                f"{base}/v1/provenance",
                json=record.model_dump(),
                headers={"X-Api-Key": api_key},
                timeout=10.0,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            console.print(f"[red]Failed to record to hosted service:[/red] {exc}")
            raise typer.Exit(code=2) from exc
        console.print(
            f"[green]Recorded[/green] {decision_id} -> {file}:{line_start}-{line_end} "
            f"(hosted, {resp.json().get('records', '?')} total)"
        )
    else:
        settings = get_settings()
        store = ProvenanceStore(settings.provenance_db)
        store.add(record)
        console.print(
            f"[green]Recorded[/green] {decision_id} -> {file}:{line_start}-{line_end} "
            f"(local {settings.provenance_db})"
        )


@app.command()
def status() -> None:
    """Show provenance store + config summary."""
    settings = get_settings()
    store = ProvenanceStore(settings.provenance_db)
    table = Table(title="CodeAutopsy status")
    table.add_column("field")
    table.add_column("value")
    table.add_row("provenance db", str(settings.provenance_db))
    table.add_row("decisions indexed", str(store.count()))
    table.add_row("provenance service", settings.provenance_url)
    table.add_row("otel endpoint", settings.otel_endpoint)
    console.print(table)


if __name__ == "__main__":
    app()

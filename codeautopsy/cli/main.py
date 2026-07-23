"""CodeAutopsy CLI — the Coroner.

`codeautopsy autopsy <commit> <file> <line>` prints the chain of custody as a coroner's
report: which AI decision authored the crashing line, its reasoning, and its risk flags.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from codeautopsy.config import get_settings
from codeautopsy.fixbot.core import FixBotError, run_fixbot
from codeautopsy.otel import force_utf8_stdout
from codeautopsy.provenance.store import ProvenanceStore
from codeautopsy.recorder.commit_indexer import index_pending_at_head

force_utf8_stdout()
app = typer.Typer(help="CodeAutopsy — git blame for the AI era.", no_args_is_help=True)
console = Console()


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
) -> None:
    """Feed the agent its own autopsy: patch the bug, prove it with a regression test, commit."""
    settings = get_settings()
    console.print(
        "[bold]Fix Bot:[/bold] resolving genealogy and asking the agent to patch itself..."
    )
    try:
        result = run_fixbot(settings, commit, file, line, push=push)
    except FixBotError as exc:
        console.print(f"[red]Fix Bot failed:[/red] {exc}")
        raise typer.Exit(code=2) from exc

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

"""Prognosis — the pre-mortem bot.

Autopsy answers "which AI decision caused this crash?" after the fact, by blaming a
runtime file:line back to its authoring commit. Prognosis asks the cheaper question
*before* merge: walk a PR's diff, resolve each added line back to a decision the same
way, and price its risk flags against the track record every decision carrying that flag
has already built up in `provenance` + `incidents` — so "assumed_valid_input" isn't just
a vibe, it's "3 of the 5 decisions carrying this flag have gone on to crash in production."

Lines with no indexed decision (human-written, or authored by a tool that never hooked
in) still get scanned for the same risk patterns directly against their raw text, via
`codeautopsy.recorder.risk.detect_risk_flags` — lower-confidence, but not nothing.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from codeautopsy.prognosis.models import FlagStats, LineFinding, PrognosisReport
from codeautopsy.provenance.indexer import blame_origin
from codeautopsy.provenance.models import ProvenanceRecord
from codeautopsy.provenance.store import ProvenanceStoreProtocol
from codeautopsy.recorder.risk import detect_risk_flags


class PrognosisError(RuntimeError):
    pass


def _git(repo: str | Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise PrognosisError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout


_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def changed_lines(
    repo: str | Path, base_ref: str, head_ref: str = "HEAD"
) -> dict[str, list[tuple[int, str]]]:
    """Diff `base_ref...head_ref`, returning ``{file_path: [(line_no, added_text), ...]}``
    for every *added* line — context and removed lines aren't actionable, there's nothing
    new on them to price. Uses the triple-dot range so the diff is against the merge-base,
    matching what a PR actually introduces rather than every commit on the base branch since.
    """
    diff = _git(repo, "diff", "--unified=0", "--no-color", f"{base_ref}...{head_ref}")
    result: dict[str, list[tuple[int, str]]] = {}
    current_file: str | None = None
    next_line = 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            current_file = None if path == "/dev/null" else path.removeprefix("b/")
            continue
        if raw.startswith("@@"):
            match = _HUNK_HEADER.match(raw)
            if match:
                next_line = int(match.group(1))
            continue
        if current_file is None:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            result.setdefault(current_file, []).append((next_line, raw[1:]))
            next_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            continue
    return result


def resolve_line(
    store: ProvenanceStoreProtocol,
    repo: str | Path,
    file_path: str,
    line: int,
    head_ref: str = "HEAD",
    org_id: str = "demo-public",
) -> ProvenanceRecord | None:
    """Blame `file_path:line` at `head_ref` to find the commit (within the PR) that
    introduced it, then look up the decision recorded against that exact commit + line —
    the same join `codeautopsy.provenance.indexer.resolve` uses for runtime crashes, just
    run against a PR's own tip instead of a deployed commit.
    """
    origin = blame_origin(repo, file_path, line, at_commit=head_ref)
    if origin is None:
        return None
    commit_sha, orig_line = origin
    return store.find_by_line(commit_sha, file_path, orig_line, org_id=org_id)


def compute_flag_stats(
    store: ProvenanceStoreProtocol, org_id: str = "demo-public"
) -> dict[str, FlagStats]:
    """For every risk flag ever recorded, what fraction of the decisions carrying it went
    on to produce at least one incident? Aggregated in Python rather than SQL — `risk_flags`
    is a JSON array packed into a TEXT column, and the store is deliberately tiny (one
    table, one index), so this mirrors that same boring-by-design choice.
    """
    decisions = store.all(org_id=org_id)
    incidents = store.list_incidents(org_id=org_id)
    crashed_decision_ids = {i.decision_id for i in incidents if i.decision_id}

    stats: dict[str, FlagStats] = {}
    for rec in decisions:
        crashed = bool(rec.decision_id) and rec.decision_id in crashed_decision_ids
        for flag in rec.risk_flags:
            entry = stats.setdefault(flag, FlagStats(flag=flag))
            entry.decisions += 1
            if crashed:
                entry.crashed_decisions += 1
    return stats


def _price(
    flags: list[str], flag_stats: dict[str, FlagStats], min_samples: int
) -> tuple[float | None, str, int]:
    """Pick the worst (highest) crash rate among `flags` that clears `min_samples`."""
    worst_rate: float | None = None
    worst_flag = ""
    worst_samples = 0
    for flag in flags:
        stat = flag_stats.get(flag)
        if stat is None or stat.decisions < min_samples:
            continue
        rate = stat.crash_rate
        if rate is not None and (worst_rate is None or rate > worst_rate):
            worst_rate, worst_flag, worst_samples = rate, flag, stat.decisions
    return worst_rate, worst_flag, worst_samples


def scan(
    store: ProvenanceStoreProtocol,
    repo: str | Path,
    base_ref: str,
    head_ref: str = "HEAD",
    org_id: str = "demo-public",
    min_samples: int = 2,
) -> PrognosisReport:
    """Walk a PR's added lines, resolve each to a decision (or fall back to a pattern
    scan), and price its risk flags against historical crash rates."""
    flag_stats = compute_flag_stats(store, org_id=org_id)
    diff = changed_lines(repo, base_ref, head_ref)

    findings: list[LineFinding] = []
    lines_scanned = 0
    for file_path, lines in diff.items():
        for line_no, text in lines:
            lines_scanned += 1
            record = resolve_line(store, repo, file_path, line_no, head_ref, org_id=org_id)
            if record is not None and record.risk_flags:
                rate, worst_flag, samples = _price(record.risk_flags, flag_stats, min_samples)
                findings.append(
                    LineFinding(
                        file_path=file_path,
                        line=line_no,
                        risk_flags=record.risk_flags,
                        reasoning_summary=record.reasoning_summary,
                        decision_id=record.decision_id,
                        source="decision",
                        crash_rate=rate,
                        worst_flag=worst_flag,
                        sample_size=samples,
                    )
                )
                continue

            pattern_flags = detect_risk_flags(text)
            if pattern_flags:
                rate, worst_flag, samples = _price(pattern_flags, flag_stats, min_samples)
                findings.append(
                    LineFinding(
                        file_path=file_path,
                        line=line_no,
                        risk_flags=pattern_flags,
                        source="pattern",
                        crash_rate=rate,
                        worst_flag=worst_flag,
                        sample_size=samples,
                    )
                )

    findings.sort(key=lambda f: (f.crash_rate is None, -(f.crash_rate or 0.0)))
    return PrognosisReport(
        base_ref=base_ref,
        head_ref=head_ref,
        lines_scanned=lines_scanned,
        findings=findings,
        flag_stats=flag_stats,
    )


def render_markdown(report: PrognosisReport, min_samples: int = 2) -> str:
    """Render the report as a PR-comment-ready Markdown body."""
    files_flagged = len({f.file_path for f in report.findings})
    lines = [
        "## Prognosis — pre-mortem risk scan",
        "",
        f"Scanned {report.lines_scanned} added line(s), flagged {len(report.findings)} across "
        f"{files_flagged} file(s), between `{report.base_ref}` and `{report.head_ref}`.",
        "",
    ]
    if not report.findings:
        lines.append("No risk flags found on any added line. Clean bill of health.")
        return "\n".join(lines)

    priced = [f for f in report.findings if f.crash_rate is not None]
    if priced:
        lines.append("| File:Line | Flag | Historical crash rate | Source | Reasoning |")
        lines.append("|---|---|---|---|---|")
        for f in priced:
            rate_str = f"**{f.crash_rate:.0%}** ({f.sample_size} past decisions)"
            reasoning = f.reasoning_summary or "_(pattern match only — no decision reasoning)_"
            lines.append(
                f"| `{f.file_path}:{f.line}` | `{f.worst_flag}` | {rate_str} "
                f"| {f.source} | {reasoning[:120]} |"
            )
        lines.append("")

    unpriced = [f for f in report.findings if f.crash_rate is None]
    if unpriced:
        lines.append(
            f"<details><summary>{len(unpriced)} more flagged line(s) — fewer than "
            f"{min_samples} historical decisions to price a crash rate</summary>"
        )
        lines.append("")
        for f in unpriced:
            lines.append(f"- `{f.file_path}:{f.line}` — {', '.join(f.risk_flags)} ({f.source})")
        lines.append("")
        lines.append("</details>")

    lines.append("")
    lines.append(
        "_Prognosis resolves each added line to its authoring AI decision via the same "
        "git-blame join CodeAutopsy uses to resolve production crashes, then prices its "
        "risk flags against every decision + incident indexed so far — CodeAutopsy, "
        "git blame for the AI era._"
    )
    return "\n".join(lines)


def post_comment(repo_root: str | Path, body: str, pr: str | None = None) -> str | None:
    """Post the report as a PR comment via `gh`. Returns the comment URL, or None if
    there's no remote / `gh` isn't installed or authenticated / no open PR — the same
    graceful-None contract as `codeautopsy.fixbot.core.open_pull_request`.
    """
    args = ["gh", "pr", "comment"]
    if pr:
        args.append(pr)
    args += ["--body", body]
    try:
        proc = subprocess.run(args, cwd=str(repo_root), capture_output=True, text=True)
    except FileNotFoundError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else None

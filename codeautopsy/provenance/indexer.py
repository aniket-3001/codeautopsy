"""The git-blame join engine.

A runtime stack frame gives `file:line`. Blame at the *deployed* commit gives the commit
that introduced that line. We then look up which AI decision authored that line range.

`git blame` is the join key of the entire product.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from codeautopsy.provenance.models import ProvenanceRecord, ResolveRequest, ResolveResponse
from codeautopsy.provenance.store import ProvenanceStore


class GitError(RuntimeError):
    pass


def _git(repo: str | Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout


def blame_origin(
    repo: str | Path, file_path: str, line: int, at_commit: str = "HEAD"
) -> tuple[str, int] | None:
    """Blame `file_path:line` as of `at_commit`, returning (introducing_sha, original_line).

    The porcelain header line is: ``<sha> <orig_line> <final_line> [<num_lines>]``.
    Returning the *original* line number is essential: line numbers drift between the commit
    that introduced a line and the commit that's deployed, so the provenance store (keyed on
    introducing-commit line numbers) must be queried with the original line, not the runtime one.
    """
    try:
        out = _git(
            repo, "blame", "-L", f"{line},{line}", "--porcelain", at_commit, "--", file_path
        )
    except GitError:
        return None
    first = out.splitlines()[0] if out else ""
    parts = first.split()
    if len(parts) < 2:
        return None
    sha = parts[0].strip()
    if not (len(sha) >= 7 and all(c in "0123456789abcdef" for c in sha)):
        return None
    try:
        orig_line = int(parts[1])
    except ValueError:
        return None
    return sha, orig_line


def blame_introducing_commit(
    repo: str | Path, file_path: str, line: int, at_commit: str = "HEAD"
) -> str | None:
    """Return just the SHA of the commit that introduced `file_path:line` as of `at_commit`."""
    origin = blame_origin(repo, file_path, line, at_commit)
    return origin[0] if origin else None


def resolve(
    store: ProvenanceStore, req: ResolveRequest, repo: str | Path | None = None
) -> ResolveResponse:
    """Resolve a runtime file:line@commit back to the AI decision that wrote it.

    Strategy:
      1. Try the deployed commit directly (fast path — recorder often tags the deploy SHA).
      2. Otherwise blame at the deployed commit to find the *introducing* commit, and match
         the decision recorded against that commit.
    """
    # Fast path: a decision recorded directly against the deployed commit.
    direct = store.find_by_line(req.commit_sha, req.file_path, req.line)
    if direct is not None:
        return ResolveResponse(
            resolved=True,
            introducing_commit=req.commit_sha,
            record=direct,
            detail="matched decision recorded at the deployed commit",
        )

    # Blame path: find which commit introduced the crashing line, and at which original line
    # (line numbers drift between the introducing commit and the deployed commit).
    if repo is not None:
        origin = blame_origin(repo, req.file_path, req.line, req.commit_sha)
        if origin:
            introducing, orig_line = origin
            rec = store.find_by_line(introducing, req.file_path, orig_line)
            if rec is not None:
                return ResolveResponse(
                    resolved=True,
                    introducing_commit=introducing,
                    record=rec,
                    detail=(
                        f"matched via git blame to {introducing[:8]} "
                        f"at original line {orig_line}"
                    ),
                )
            return ResolveResponse(
                resolved=False,
                introducing_commit=introducing,
                detail="blame found the introducing commit but no AI decision is indexed for it",
            )

    return ResolveResponse(resolved=False, detail="no matching provenance and no repo to blame")


def index_records(store: ProvenanceStore, records: list[ProvenanceRecord]) -> int:
    """Bulk-load decisions into the store (used by the recorder after a commit)."""
    return store.add_many(records)

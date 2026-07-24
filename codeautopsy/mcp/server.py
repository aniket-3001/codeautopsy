"""stdio MCP server exposing CodeAutopsy's tools. Entry point: `codeautopsy-mcp`.

Wire it into an MCP client (Cursor, Claude Desktop, …) as a server whose command is
`codeautopsy-mcp`. The agent then gets three CodeAutopsy tools on its menu — see `core.py`
for the logic each one runs.

Install with the extra so the `mcp` package is present:  `pip install -e ".[mcp]"`.
"""

from __future__ import annotations

from typing import Any

from codeautopsy.mcp import core


def build_server() -> Any:
    """Construct the FastMCP server with CodeAutopsy's tools registered.

    `mcp` is an optional dependency (the `mcp` extra), so it is imported here rather than at
    module load — importing this module stays cheap and never hard-requires the package.
    """
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("codeautopsy")

    @server.tool()
    def autopsy(commit_sha: str, file_path: str, line: int, repo: str | None = None) -> dict:
        """Which AI coding decision authored a crashing line?

        Given a runtime crash coordinate — the deployed commit SHA, a repo-relative file path,
        and the crashing line number — blame the line back to its introducing commit and return
        the AI agent decision that wrote it: the reasoning summary, the authoring tool/model,
        the risk flags raised at authoring time, and the decision's trace/span ids in SigNoz.
        `repo` defaults to the configured target repo.
        """
        return core.autopsy(commit_sha, file_path, line, repo=repo)

    @server.tool()
    def prognose(code: str, reasoning: str = "") -> dict:
        """Price a code snippet's risk against real production crash history.

        Detects risk flags in the snippet and prices each against this project's own recorded
        incidents — a pre-merge second opinion grounded in what has actually crashed before,
        not a model's guess. Returns a verdict (clear / flagged / priced) with per-flag rates.
        """
        return core.prognose(code, reasoning)

    @server.tool()
    def leaderboard() -> dict:
        """Rank the AI tools/models used in this project by real production crash rate.

        The retrospective scoreboard: every tool/model that has authored recorded decisions,
        ranked by how often its decisions ended up in a production incident.
        """
        return core.leaderboard()

    return server


def run() -> None:
    """Run the server over stdio (the transport MCP clients launch)."""
    build_server().run()


if __name__ == "__main__":
    run()

"""stdio MCP server exposing CodeAutopsy's tools. Entry point: `codeautopsy-mcp`.

Wire it into an MCP client (Cursor, Claude Desktop, …) as a server whose command is
`codeautopsy-mcp`. The agent then gets four CodeAutopsy tools on its menu — see `core.py`
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
    def autopsy(
        commit_sha: str, file_path: str, line: int, repo: str | None = None
    ) -> dict[str, Any]:
        """Which AI coding decision authored a crashing line?

        Given a runtime crash coordinate — the deployed commit SHA, a repo-relative file path,
        and the crashing line number — blame the line back to its introducing commit and return
        the AI agent decision that wrote it: the reasoning summary, the authoring tool/model,
        the risk flags raised at authoring time, and the decision's trace/span ids in SigNoz.
        `repo` defaults to the configured target repo.
        """
        return core.autopsy(commit_sha, file_path, line, repo=repo)

    @server.tool()
    def prognose(code: str, reasoning: str = "") -> dict[str, Any]:
        """Price a code snippet's risk against real production crash history.

        Detects risk flags in the snippet and prices each against this project's own recorded
        incidents — a pre-merge second opinion grounded in what has actually crashed before,
        not a model's guess. Returns a verdict (clear / flagged / priced) with per-flag rates.
        """
        return core.prognose(code, reasoning)

    @server.tool()
    def postmortem(commit_sha: str, file_path: str, line: int) -> dict[str, Any]:
        """Render the full chain-of-custody postmortem for a crash as shareable markdown.

        Assembles the same document `codeautopsy report` prints on the CLI: crash -> cause of
        death -> blame -> decision -> reasoning -> confidence -> lesson learned (if this class
        of bug has struck before). Useful when asked to write an incident summary or a PR
        description for a fix.
        """
        return core.postmortem(commit_sha, file_path, line)

    @server.tool()
    def leaderboard() -> dict[str, Any]:
        """Rank the AI tools/models used in this project by real production crash rate.

        The retrospective scoreboard: every tool/model that has authored recorded decisions,
        ranked by how often its decisions ended up in a production incident.
        """
        return core.leaderboard()

    return server


def run() -> None:  # pragma: no cover
    """Run the server over stdio (the transport MCP clients launch).

    Bootstraps a real TracerProvider here — not at module import time, so importing this
    module for `build_server()` alone (as `test_server_registers_four_tools` does) stays
    free of OTel side effects, matching the "importing this module stays cheap" contract
    documented above. Every call an MCP client makes into
    `autopsy`/`prognose`/`postmortem`/`leaderboard` (`codeautopsy/mcp/core.py`) is now itself a
    span in the same SigNoz pipeline the rest of CodeAutopsy exports to.

    Not unit-tested: this blocks on the real stdio transport, which only makes sense under
    an actual MCP client. `build_server()`'s tool wiring is covered directly (see test_mcp.py).
    """
    from opentelemetry import trace

    from codeautopsy.otel import build_tracer_provider

    trace.set_tracer_provider(build_tracer_provider("codeautopsy-mcp"))
    build_server().run()


if __name__ == "__main__":
    run()

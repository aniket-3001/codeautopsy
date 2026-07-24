"""CodeAutopsy as an MCP server.

Most Agents-of-SigNoz entries *consume* SigNoz's MCP server — their agent reads telemetry
through it. CodeAutopsy points the plug the other way: it *is* an MCP server, exposing the one
thing only CodeAutopsy knows as agent-callable tools, so any MCP client (Cursor, Claude
Desktop, an IDE) can ask:

    autopsy(commit, file, line)  -> the AI decision that authored a crashing line
    prognose(code)               -> that code's risk, priced against real crash history
    leaderboard()                -> which AI tools/models crash most in production

`core` holds the pure, `mcp`-package-free tool logic (unit-tested directly); `server` is the
thin FastMCP wiring behind the `codeautopsy-mcp` entry point.
"""

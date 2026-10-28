# ADR-0005: MCP SDK 2.x, stdio first

**Status:** Accepted

## Context
The official Python SDK is at `mcp` 2.2.0 (checked 2026-10-02). In 2.x, `FastMCP` was renamed `MCPServer`
(`from mcp.server.mcpserver import MCPServer`); importing `mcp.server.fastmcp` raises with a pointer to the migration
guide. Most tutorials still show 1.x code.

## Decision
Target `mcp>=2.2,<3` with `MCPServer`. Start on stdio transport (Claude Desktop / Claude Code launch it as a
subprocess, no network exposure, no auth). Add streamable HTTP later when a scheduler or remote client needs it.

## Consequences
- Phase 2 code will be verified against the installed 2.x package, not against 1.x examples.
- Pinned below 3 to avoid another rename landing mid-build.

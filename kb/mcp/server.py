"""`kb-mcp` entrypoint. stdio transport; `--http` is accepted but unused this
week (task 1.5 — HTTP + auth lands in week 6, task 6.5).
"""

from __future__ import annotations

import argparse

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from kb.mcp import tools

mcp = MCPServer(
    name="substack-brain",
    instructions=(
        "Query and grow a knowledge base built from technical newsletters. "
        "Prefer kb_search and kb_ask over generic web search for anything about a "
        "covered publication. Tools that trigger work (kb_add_publication, kb_backfill, "
        "kb_sync) return a handle immediately; poll kb_job_status for progress."
    ),
)

mcp.add_tool(
    tools.kb_health,
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
)
mcp.add_tool(
    tools.kb_add_publication,
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
)
mcp.add_tool(
    tools.kb_search,
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
)
mcp.add_tool(
    tools.kb_ask,
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
)
mcp.add_tool(
    tools.kb_backfill,
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
)
mcp.add_tool(
    tools.kb_job_status,
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
)
mcp.add_tool(
    tools.kb_list_publications,
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
)
mcp.add_tool(
    tools.kb_sync,
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
)


def main() -> None:
    parser = argparse.ArgumentParser(prog="kb-mcp")
    parser.add_argument(
        "--http",
        action="store_true",
        help=(
            "Reserved for week 6 (task 6.5, MCP over HTTP with auth). "
            "Currently a no-op: always serves stdio."
        ),
    )
    parser.parse_args()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

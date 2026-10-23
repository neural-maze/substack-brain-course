"""Protocol-level check: the MCP server actually serves its tools correctly.

Connects an in-process `mcp.client.client.Client` directly to the real
`MCPServer` instance (no subprocess) — this exercises the real MCP protocol
(tool listing, JSON schema, `call_tool` request/response), which calling the
tool functions directly (see test_mcp_tools.py) does not.
"""

from __future__ import annotations

import json

import httpx
import pytest
from mcp.client.client import Client
from mcp.types import TextContent

from kb.config import settings
from kb.mcp.server import mcp


@pytest.fixture(autouse=True)
def _require_dev_stack() -> None:
    try:
        with httpx.Client(timeout=2.0) as client:
            client.get(settings.inngest_base_url)
    except httpx.HTTPError:
        pytest.skip(
            f"Inngest Dev Server not reachable at {settings.inngest_base_url} — "
            "run `make start` first."
        )


async def test_lists_all_eight_week_three_tools() -> None:
    async with Client(server=mcp) as client:
        result = await client.list_tools()
    names = {t.name for t in result.tools}
    assert names == {
        "kb_health",
        "kb_add_publication",
        "kb_search",
        "kb_ask",
        "kb_backfill",
        "kb_job_status",
        "kb_list_publications",
        "kb_sync",
        "kb_sources",
        "kb_eval_report",
    }


async def test_kb_search_schema_enforces_the_limit_bounds() -> None:
    async with Client(server=mcp) as client:
        result = await client.list_tools()
    kb_search = next(t for t in result.tools if t.name == "kb_search")
    limit_schema = kb_search.input_schema["properties"]["limit"]
    assert limit_schema["minimum"] == 1
    assert limit_schema["maximum"] == 25


async def test_call_kb_health_over_the_real_protocol() -> None:
    async with Client(server=mcp) as client:
        result = await client.call_tool("kb_health", {})
    assert result.is_error is not True
    first = result.content[0]
    assert isinstance(first, TextContent)
    payload = json.loads(first.text)
    assert payload["status"] == "ok"

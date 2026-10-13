"""Week 3 tests: conditional polling, supersession, failure tracking, and MCP tools.

Like test_basics.py and test_week2.py, these unit tests verify pure logic and contracts
without requiring external network or live Docker services.
"""

from mcp.client.client import Client

from kb.functions.freshness import poll_feeds
from kb.functions.ops import handle_function_failed
from kb.mcp.server import mcp
from kb.schemas.events import ArticleUpdated


def test_mcp_server_registers_all_eight_tools() -> None:
    tools_dict = mcp._tool_manager._tools
    expected_names = {
        "kb_health",
        "kb_add_publication",
        "kb_search",
        "kb_ask",
        "kb_backfill",
        "kb_job_status",
        "kb_list_publications",
        "kb_sync",
    }
    assert set(tools_dict.keys()) == expected_names


async def test_mcp_server_schema_and_annotations() -> None:
    async with Client(server=mcp) as client:
        result = await client.list_tools()

    tool_names = {t.name for t in result.tools}
    assert "kb_search" in tool_names
    assert "kb_ask" in tool_names
    assert "kb_sync" in tool_names
    assert "kb_backfill" in tool_names

    search_tool = next(t for t in result.tools if t.name == "kb_search")
    assert search_tool.input_schema["properties"]["limit"]["minimum"] == 1
    assert search_tool.input_schema["properties"]["limit"]["maximum"] == 25


def test_freshness_function_triggers() -> None:
    config = poll_feeds.get_config("http://localhost:8000").main
    triggers = config.triggers
    assert len(triggers) == 2
    # Cron trigger + Event trigger (kb/feed.poll)
    cron_triggers = [t for t in triggers if hasattr(t, "cron")]
    event_triggers = [t for t in triggers if hasattr(t, "event")]
    assert any(t.cron == "*/5 * * * *" for t in cron_triggers)
    assert any(t.event == "kb/feed.poll" for t in event_triggers)


def test_ops_function_trigger() -> None:
    config = handle_function_failed.get_config("http://localhost:8000").main
    triggers = config.triggers
    assert len(triggers) == 1
    assert getattr(triggers[0], "event", None) == "inngest/function.failed"


def test_article_updated_event_schema() -> None:
    import uuid

    aid = uuid.uuid4()
    event = ArticleUpdated(
        canonical_id="pub:test:post-1",
        article_id=aid,
        content_hash="abc123hash",
        passage_count=4,
    )
    dumped = event.model_dump(mode="json")
    assert dumped["canonical_id"] == "pub:test:post-1"
    assert dumped["article_id"] == str(aid)
    assert dumped["passage_count"] == 4

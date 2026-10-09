"""Integration tests for the week-3 MCP tools.

`kb_search` runs against a real Postgres, rolled back per test (`db`
fixture). `kb_health` and `kb_job_status` are deliberately left unmocked —
they exist to reflect live reachability of Postgres/the Inngest Dev Server,
so testing them against the real local dev stack is the point. Skipped automatically
if that stack isn't up.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from kb.config import settings
from kb.db.queries import replace_passages, upsert_article, upsert_publication
from kb.mcp import tools


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


async def test_kb_health_reports_ok_against_the_real_dev_stack() -> None:
    result = await tools.kb_health()
    assert result["status"] == "ok"
    assert result["database"] == "ok"
    assert result["inngest"] == "ok"
    assert isinstance(result["articles"], int)
    assert isinstance(result["passages"], int)


async def test_kb_search_finds_a_seeded_passage(db) -> None:
    publication_id = await upsert_publication(
        db,
        slug="kb-search-test-pub",
        name="Test Pub",
        feed_url="https://search-test.example.com/feed",
        homepage="https://search-test.example.com",
    )
    article_id = await upsert_article(
        db,
        canonical_id="pub:kb-search-test-pub:only-post",
        publication_id=publication_id,
        url="https://search-test.example.com/p/only-post",
        title="A Post About Zebras",
        author="Test Author",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        content_hash="hash",
        text="Zebras are fascinating striped animals found in Africa.",
    )
    await replace_passages(
        db,
        article_id=article_id,
        passages=[
            {
                "chunk_index": 0,
                "text": "Zebras are fascinating striped animals found in Africa.",
                "embedding": None,
            }
        ],
    )
    await db.commit()

    result = await tools.kb_search("striped zebras Africa", limit=5)

    assert len(result["results"]) >= 1
    titles = [r["title"] for r in result["results"]]
    assert "A Post About Zebras" in titles


async def test_kb_search_returns_empty_not_an_error_when_nothing_matches() -> None:
    result = await tools.kb_search("a query that matches absolutely nothing zzyzx", limit=5)
    assert result == {"results": []}


async def test_kb_job_status_raises_for_an_unknown_event_id() -> None:
    with pytest.raises(ToolError, match="No job or Inngest run found"):
        await tools.kb_job_status("not-a-real-event-id")


async def test_kb_list_publications_returns_list() -> None:
    result = await tools.kb_list_publications()
    assert "publications" in result
    assert isinstance(result["publications"], list)

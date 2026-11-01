"""Integration tests for Week 6 bounded research runs and weekly digests against real Postgres:

1. `upsert_research_run` and `get_research_run` persist and retrieve hop traces,
   citations, evaluation summaries, and USD cost totals.
2. `insert_digest` and `get_latest_digest` persist and retrieve weekly briefings.
3. `kb_research_result` and `kb_weekly_digest` MCP tools read from Postgres.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from kb.config import settings
from kb.db.queries import (
    get_latest_digest,
    get_research_run,
    insert_digest,
    upsert_research_run,
)
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


async def test_research_run_and_digest_persistence_and_mcp_reads(
    db: AsyncSession,
) -> None:
    rid = "res_week6_test_01"
    await upsert_research_run(
        db,
        research_id=rid,
        event_id="evt_test_01",
        question="How does 3-way RRF compare to 2-way RRF across publications?",
        status="completed",
        plan=["3-way RRF vs 2-way RRF", "Memgraph claim traversal"],
        hops=[
            {
                "hop": 1,
                "queries": ["3-way RRF vs 2-way RRF"],
                "newly_added_passages": 3,
                "cumulative_passages": 3,
                "decision": "sufficient",
                "rationale": "Retrieved 3 grounded passages.",
                "step_cost_usd": 0.0021,
            }
        ],
        report_text="Three-way RRF lifts Recall@5 to 0.94 [1].",
        citations=[
            {
                "citation_idx": 1,
                "passage_id": "p-1",
                "title": "Graph Fusion",
                "author": "Miguel Otero Pedrido",
                "url": "https://www.theneuralmaze.com/p/graph-fusion",
            }
        ],
        retrieved=[
            {
                "passage_id": "p-1",
                "title": "Graph Fusion",
                "text": "Three-way RRF lifts Recall@5 to 0.94.",
            }
        ],
        eval_summary={
            "deterministic_passed": True,
            "faithfulness_verdict": "PASS",
            "disagreement_verdict": "NOT_APPLICABLE",
            "failure_codes": [],
            "composite_score": 1.0,
        },
        confidence=1.0,
        iterations_used=1,
        total_cost_usd=0.0045,
        latency_ms=420,
        completed_at=datetime.now(UTC),
    )

    digest_id = uuid.uuid4()
    now = datetime.now(UTC)
    await insert_digest(
        db,
        digest_id=digest_id,
        window_start=now,
        window_end=now,
        title="The Substack Brain Weekly Digest (Test)",
        summary_markdown="# Weekly Digest\n\n- **Miguel**: 3-way RRF reaches 0.94 Recall@5 [1].",
        citations=[
            {
                "citation_idx": 1,
                "passage_id": "p-1",
                "title": "Graph Fusion",
                "author": "Miguel Otero Pedrido",
                "url": "https://www.theneuralmaze.com/p/graph-fusion",
            }
        ],
        stats={"articles_count": 3, "claims_count": 9, "deterministic_passed": True},
    )
    await db.commit()

    stored_run = await get_research_run(db, rid)
    assert stored_run is not None
    assert stored_run["status"] == "completed"
    assert stored_run["confidence"] == 1.0

    mcp_run = await tools.kb_research_result(rid)
    assert mcp_run["research_id"] == rid
    assert mcp_run["iterations_used"] == 1

    stored_digest = await get_latest_digest(db)
    assert stored_digest is not None
    assert stored_digest["title"] == "The Substack Brain Weekly Digest (Test)"

    mcp_digest = await tools.kb_weekly_digest()
    assert mcp_digest["title"] == "The Substack Brain Weekly Digest (Test)"


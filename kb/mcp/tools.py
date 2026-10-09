"""The eight week-3 MCP tools.

Fast reads (`kb_health`, `kb_search`, `kb_ask`, `kb_list_publications`) hit
Postgres and retrieval engines directly and return in under a second — no Inngest
run is created for them, preserving latency.
Slow work (`kb_add_publication`, `kb_backfill`, `kb_sync`) sends an event to
Inngest and returns a handle immediately; progress is polled with `kb_job_status`.
"""

from __future__ import annotations

import typing

import httpx
import inngest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from kb.config import settings
from kb.db.queries import get_job_status, list_publications_with_stats
from kb.db.session import get_session
from kb.functions.ask import ask_question
from kb.health import check_health
from kb.inngest_client import client
from kb.retrieval import hybrid
from kb.schemas.events import PUBLICATION_ADDED, PublicationAdded
from kb.sources.allowlist import load_publications


async def kb_health() -> dict[str, object]:
    """Check whether Postgres and the Inngest Dev Server are reachable.

    Returns article/passage counts alongside per-subsystem status
    (`"ok"` or `"error: <detail>"`). Call this first if any other tool call
    fails unexpectedly — it tells you which dependency is actually down.
    """
    return await check_health()


async def kb_add_publication(
    feed_url: typing.Annotated[
        str,
        Field(description="The publication's RSS feed URL, e.g. https://example.substack.com/feed"),
    ],
) -> dict[str, object]:
    """Add a publication by its RSS feed URL and start ingesting its articles.

    The feed's host must already be listed in this repo's `publications.yaml`
    allowlist — this tool refuses (via a fast, deterministic Inngest rejection)
    anything outside it rather than silently ingesting an unapproved source.

    Returns immediately with `{event_id, trace_url, status}`; it does not wait
    for ingestion to finish. Poll `kb_job_status(event_id)` with the returned
    `event_id` to see progress, and use `trace_url` to inspect the run in the
    Inngest Dev Server UI.
    """
    payload = PublicationAdded(feed_url=feed_url)
    event = inngest.Event(name=PUBLICATION_ADDED, data=payload.model_dump(mode="json"))
    event_ids = await client.send(event)
    event_id = event_ids[0]
    return {
        "event_id": event_id,
        "trace_url": f"{settings.inngest_base_url}/event/{event_id}",
        "status": "queued",
    }


async def kb_search(
    query: typing.Annotated[
        str, Field(description="Free-text search query, e.g. 'agentic RAG evals'")
    ],
    limit: typing.Annotated[
        int, Field(default=5, ge=1, le=25, description="Max passages to return (1-25)")
    ] = 5,
) -> dict[str, object]:
    """Hybrid (sparse + dense, RRF-fused) search over ingested passages.

    Combines full-text BM25 search with pgvector semantic similarity using
    Reciprocal Rank Fusion. Returns real, attributed passages with title,
    author, URL, date, and text snippet. Returns `{"results": []}` when
    nothing matches.
    """
    async with get_session() as session:
        results = await hybrid.search(session, query, limit)
    formatted = [
        {
            **r,
            "snippet": r["text"][:300] if len(r["text"]) > 300 else r["text"],
        }
        for r in results
    ]
    return {"results": formatted}


async def kb_ask(
    question: typing.Annotated[
        str, Field(description="Technical question to answer from the knowledge base")
    ],
) -> dict[str, object]:
    """Answer a technical question using retrieved knowledge base passages with inline citations.

    Fast read path: directly performs hybrid retrieval and LLM synthesis with
    inline citations and as-of freshness timestamp. Never creates an Inngest run.
    Records a retrieval snapshot for evaluation.
    """
    async with get_session() as session:
        return await ask_question(session, question=question)


async def kb_backfill(
    publication: typing.Annotated[
        str,
        Field(
            default="all",
            description=(
                "Publication slug, feed URL, or 'all' to backfill all publications"
            ),
        ),
    ] = "all",
) -> dict[str, object]:
    """Trigger a backfill ingestion run for one publication or all allowlisted publications.

    Discovers all available posts from the RSS feed, skipping already ingested
    articles, and queues ingestion jobs. Returns immediately with the primary
    event_id and trace_url.
    """
    pubs = load_publications()
    if publication != "all":
        matched = [p for p in pubs if p.slug == publication or p.feed_url == publication]
        if not matched:
            raise ToolError(
                f"Publication {publication!r} not found in publications.yaml allowlist."
            )
        target_pubs = matched
    else:
        target_pubs = pubs

    events = [
        inngest.Event(
            name=PUBLICATION_ADDED,
            data=PublicationAdded(feed_url=p.feed_url).model_dump(mode="json"),
        )
        for p in target_pubs
    ]
    event_ids = await client.send(events)
    first_id = event_ids[0]
    return {
        "event_id": first_id,
        "trace_url": f"{settings.inngest_base_url}/event/{first_id}",
        "expected_articles": len(target_pubs) * 20,
        "publications": [p.slug for p in target_pubs],
    }


async def kb_job_status(
    event_id: typing.Annotated[
        str, Field(description="The event_id returned by kb_add_publication or kb_backfill")
    ],
) -> dict[str, object]:
    """Check progress of a publication ingestion or backfill job.

    Combines this repo's own `jobs` row (article counts) with the Inngest Dev
    Server's real run status and step execution details for that event.
    """
    trace_url = f"{settings.inngest_base_url}/event/{event_id}"

    run_status: str | None = None
    run_output: object = None
    steps_completed: int = 0
    try:
        async with httpx.AsyncClient(timeout=5.0) as http_client:
            runs_url = f"{settings.inngest_base_url}/v1/events/{event_id}/runs"
            response = await http_client.get(runs_url)
    except httpx.HTTPError as exc:
        raise ToolError(
            f"Could not reach the Inngest Dev Server at {settings.inngest_base_url}: {exc}"
        ) from exc

    if response.status_code == 200:
        runs = response.json().get("data", [])
        if runs:
            run_status = runs[0]["status"]
            run_output = runs[0].get("output")
            steps = runs[0].get("steps") or {}
            steps_completed = len(steps)

    async with get_session() as session:
        job = await get_job_status(session, event_id)
        await session.commit()

    if job is None and run_status is None:
        raise ToolError(
            f"No job or Inngest run found for event_id={event_id!r}. "
            "Check that it's the id returned by kb_add_publication or kb_backfill."
        )

    return {
        "status": job["status"] if job is not None else run_status,
        "steps_completed": steps_completed,
        "articles_ingested": job["done"] if job is not None else 0,
        "articles_done": job["done"] if job is not None else None,
        "articles_total": job["total"] if job is not None else None,
        "run_status": run_status,
        "failures": [run_output] if run_status == "Failed" else [],
        "trace_url": trace_url,
    }


async def kb_list_publications() -> dict[str, object]:
    """List all publications in the knowledge base alongside article counts and sync metadata."""
    async with get_session() as session:
        pubs = await list_publications_with_stats(session)
    return {"publications": pubs}


async def kb_sync(
    publication: typing.Annotated[
        str | None,
        Field(
            default=None,
            description="Optional publication slug to sync, or None to poll all feeds",
        ),
    ] = None,
) -> dict[str, object]:
    """Force an immediate freshness poll instead of waiting for the 5-minute cron.

    Dispatches `kb/feed.poll` to Inngest to check HTTP conditional headers
    (ETag / Last-Modified) against origins and discover new articles immediately.
    """
    event = inngest.Event(
        name="kb/feed.poll",
        data={"publication": publication} if publication else {},
    )
    event_ids = await client.send(event)
    event_id = event_ids[0]
    return {
        "event_id": event_id,
        "trace_url": f"{settings.inngest_base_url}/event/{event_id}",
        "status": "poll_triggered",
    }

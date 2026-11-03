"""FastAPI app: serves Inngest functions, plus plain HTTP endpoints for
ingestion and retrieval. Everything is triggered this way, the way
Inngest's own docs describe: send an event, get a handle back, poll for status.

Writes (`POST /publications`, `POST /articles`) only send an event and return.
Reads (`GET /search`, `POST /ask`) never go through Inngest: they query
Postgres directly, because someone is waiting for them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
import inngest
import inngest.fast_api
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from kb.config import settings
from kb.db.queries import (
    get_job_status,
    get_latest_digest,
    get_latest_eval_report,
    get_research_run,
    get_source_provenance,
    upsert_publication,
)
from kb.db.session import get_session
from kb.functions import FUNCTIONS
from kb.functions.ask import ask_question
from kb.health import check_health
from kb.inngest_client import client
from kb.retrieval import dense, hybrid, sparse
from kb.schemas.events import (
    ARTICLE_DISCOVERED,
    DIGEST_REQUESTED,
    EVAL_RUN_REQUESTED,
    PUBLICATION_ADDED,
    RESEARCH_REQUESTED,
    ArticleDiscovered,
    DigestRequested,
    EvalRunRequested,
    PublicationAdded,
    ResearchRequested,
)
from kb.schemas.ids import canonical_id, research_id
from kb.sources.allowlist import find_publication_for_url, load_publications


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """When running against Inngest Cloud (`INNGEST_DEV=0`), establish an outbound
    WebSocket worker connection (`inngest.connect`) so Inngest Cloud can sync and
    invoke all 9 functions without needing ngrok, cloudflared, or public ingress.
    """
    conn: Any = None
    connect_task: asyncio.Task[None] | None = None
    if not settings.inngest_dev and settings.inngest_signing_key:
        from inngest.connect import ConnectionState, connect

        logger = logging.getLogger("uvicorn.error")
        conn = connect([(client, list(FUNCTIONS))], shutdown_signals=[])
        connect_task = asyncio.create_task(conn.start())

        async def _log_when_active() -> None:
            await conn.wait_for_state(ConnectionState.ACTIVE)
            logger.info(
                "Inngest Cloud Connect active (app=%s, functions=%d)",
                client.app_id,
                len(FUNCTIONS),
            )

        asyncio.create_task(_log_when_active())
    try:
        yield
    finally:
        if conn is not None:
            await conn.close(wait=True)
        if connect_task is not None and not connect_task.done():
            connect_task.cancel()


app = FastAPI(title="The Substack Brain", lifespan=_lifespan)

inngest.fast_api.serve(app, client, FUNCTIONS)


@app.get("/health")
async def health() -> dict[str, Any]:
    return await check_health()


@app.post("/publications")
async def add_publication(payload: PublicationAdded) -> dict[str, Any]:
    """Kick off ingestion for one publication. The feed's host must already be
    listed in `publications.yaml` — `backfill-publication` rejects anything else.

    Returns immediately with a handle; poll `GET /jobs/{event_id}` for progress.
    """
    event = inngest.Event(name=PUBLICATION_ADDED, data=payload.model_dump(mode="json"))
    event_ids = await client.send(event)
    event_id = event_ids[0]
    return {
        "event_id": event_id,
        "trace_url": f"{settings.inngest_base_url}/event/{event_id}",
        "status": "queued",
    }


class ArticleManual(BaseModel):
    url: str
    title: str | None = None


@app.post("/articles")
async def add_manual_article(payload: ArticleManual) -> dict[str, Any]:
    """Ingest one article now, ahead of any queued backfill.

    The event is tagged `via="manual"`, which `ingest-article`'s priority
    expression turns into a +`priority_manual_s` time shift: Inngest schedules
    the run as if it had arrived that many seconds earlier, so it takes the next
    free slot for its publication. The URL's host must be in `publications.yaml`.
    """
    pub = find_publication_for_url(payload.url, load_publications())
    if pub is None:
        raise HTTPException(
            status_code=400,
            detail=f"Article URL is outside the allowlist (publications.yaml): {payload.url!r}",
        )

    async with get_session() as session:
        publication_id = await upsert_publication(
            session,
            slug=pub.slug,
            name=pub.name,
            feed_url=pub.feed_url,
            homepage=pub.homepage,
        )
        await session.commit()

    event = inngest.Event(
        name=ARTICLE_DISCOVERED,
        data=ArticleDiscovered(
            canonical_id=canonical_id(pub.slug, payload.url),
            publication_id=publication_id,
            url=payload.url,
            title=payload.title or "Manual ingestion",
            # Only a fallback: the parse step uses the article's real date if it finds one.
            published_at=datetime.now(UTC),
            via="manual",
        ).model_dump(mode="json"),
    )
    event_ids = await client.send(event)
    event_id = event_ids[0]
    return {
        "event_id": event_id,
        "trace_url": f"{settings.inngest_base_url}/event/{event_id}",
        "status": "queued",
        "priority_offset_s": settings.priority_manual_s,
    }


@app.get("/jobs/{event_id}")
async def job_status(event_id: str) -> dict[str, Any]:
    """Combine this repo's own `jobs` row (article counts) with the Inngest Dev
    Server's real run status for the event that started the job.
    """
    trace_url = f"{settings.inngest_base_url}/event/{event_id}"

    run_status: str | None = None
    run_output: object = None
    headers: dict[str, str] = {}
    if settings.inngest_signing_key:
        headers["Authorization"] = f"Bearer {settings.inngest_signing_key}"
    async with httpx.AsyncClient(timeout=5.0) as http_client:
        try:
            response = await http_client.get(
                f"{settings.inngest_base_url}/v1/events/{event_id}/runs",
                headers=headers,
            )
        except httpx.HTTPError as exc:
            detail = f"Could not reach the Inngest Dev Server at {settings.inngest_base_url}: {exc}"
            raise HTTPException(status_code=502, detail=detail) from exc
    if response.status_code == 200:
        runs = response.json().get("data", [])
        if runs:
            run_status = runs[0]["status"]
            run_output = runs[0].get("output")

    async with get_session() as session:
        job = await get_job_status(session, event_id)
        await session.commit()  # persists get_job_status's read-repair of jobs.done/status

    if job is None and run_status is None:
        raise HTTPException(
            status_code=404,
            detail=f"No job or Inngest run found for event_id={event_id!r}.",
        )

    return {
        "status": job["status"] if job is not None else run_status,
        "articles_done": job["done"] if job is not None else None,
        "articles_total": job["total"] if job is not None else None,
        "run_status": run_status,
        # Only surfaces the discovery run's own top-level error.
        "failures": [run_output] if run_status == "Failed" else [],
        "trace_url": trace_url,
    }


@app.get("/search")
async def search(
    q: str,
    mode: Literal["hybrid", "sparse", "dense"] = "hybrid",
    limit: int = Query(default=5, ge=1, le=25),
) -> dict[str, Any]:
    """Search passages, no LLM. `hybrid` (the default from week 2) fuses full-text
    and vector search with Reciprocal Rank Fusion; `sparse` and `dense` query one
    engine each, so you can compare all three on the same query.
    """
    async with get_session() as session:
        if mode == "hybrid":
            results = await hybrid.search(session, q, limit)
        elif mode == "sparse":
            results = await sparse.search(session, q, limit)
        else:
            results = await dense.search(session, q, limit)
    return {"mode": mode, "results": results}


class AskRequest(BaseModel):
    question: str
    limit: int | None = None


@app.post("/ask")
async def ask(payload: AskRequest) -> dict[str, Any]:
    """Answer a question with inline citations. A fast read: it never creates an
    Inngest run. It saves a snapshot and sends `kb/answer.produced` on its way out.
    """
    async with get_session() as session:
        return await ask_question(session, question=payload.question, limit=payload.limit)


@app.get("/sources/{claim_or_passage_id}")
async def get_source(claim_or_passage_id: str) -> dict[str, Any]:
    """Resolve full provenance, verbatim quote, and temporal validity for a
    `claim_id` or `passage_id`.
    """
    try:
        target_uuid = uuid.UUID(claim_or_passage_id.strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid UUID for claim_or_passage_id={claim_or_passage_id!r}.",
        ) from exc

    async with get_session() as session:
        provenance = await get_source_provenance(session, target_uuid)

    if provenance is None:
        raise HTTPException(
            status_code=404,
            detail=f"No claim or passage found for id={claim_or_passage_id!r}.",
        )
    return provenance


@app.post("/evals/run")
async def trigger_eval_suite(payload: EvalRunRequested | None = None) -> dict[str, Any]:
    """Trigger the Week 5 multi-variant evaluation suite (`run-eval-suite`) via Inngest.

    Slow work: dispatches `kb/eval.run` and returns an `{event_id, trace_url, status}`
    handle in <50ms. Poll `GET /jobs/{event_id}` or `GET /evals/latest` for results.
    """
    req = payload or EvalRunRequested()
    event = inngest.Event(name=EVAL_RUN_REQUESTED, data=req.model_dump(mode="json"))
    event_ids = await client.send(event)
    event_id = event_ids[0]
    return {
        "event_id": event_id,
        "trace_url": f"{settings.inngest_base_url}/event/{event_id}",
        "suite_name": req.suite_name,
        "status": "queued",
    }


@app.get("/evals/latest")
async def get_eval_report(
    limit: int = Query(default=10, ge=1, le=50),
) -> dict[str, Any]:
    """Fast read (<1s): return the latest evaluation report, failure taxonomy counts,
    recent snapshot critiques, and multi-variant experiment summary.
    """
    async with get_session() as session:
        return await get_latest_eval_report(session, limit=limit)


class ResearchCreateRequest(BaseModel):
    question: str
    max_iterations: int | None = None
    max_usd: float | None = None


@app.post("/research")
async def start_research(payload: ResearchCreateRequest) -> dict[str, Any]:
    """Queue a bounded multi-hop research agent run (`run-research-agent`).

    Slow work: emits `kb/research.requested` and returns `{research_id, event_id,
    trace_url, status}` in <50ms. Poll `GET /research/{research_id}` for the report.
    """
    rid = research_id()
    req = ResearchRequested(
        research_id=rid,
        question=payload.question,
        max_iterations=payload.max_iterations,
        max_usd=payload.max_usd,
    )
    event = inngest.Event(name=RESEARCH_REQUESTED, data=req.model_dump(mode="json"))
    event_ids = await client.send(event)
    event_id = event_ids[0]
    return {
        "research_id": rid,
        "event_id": event_id,
        "trace_url": f"{settings.inngest_base_url}/event/{event_id}",
        "status": "queued",
    }


@app.get("/research/{research_id_param}")
async def get_research(research_id_param: str) -> dict[str, Any]:
    """Fast read (<1s): return the bounded research agent run, hop trace, citations,
    and evaluation summary from Postgres.
    """
    async with get_session() as session:
        run_data = await get_research_run(session, research_id_param.strip())
    if run_data is None:
        raise HTTPException(
            status_code=404,
            detail=f"No research run found for research_id={research_id_param!r}.",
        )
    return run_data


@app.post("/digests/run")
async def trigger_weekly_digest(payload: DigestRequested | None = None) -> dict[str, Any]:
    """Trigger `generate-weekly-digest` on demand via Inngest.

    Slow work: emits `kb/digest.requested` and returns an `{event_id, trace_url, status}`
    handle in <50ms. Fetch the result via `GET /digests/latest`.
    """
    req = payload or DigestRequested(lookback_days=settings.digest_lookback_days)
    event = inngest.Event(name=DIGEST_REQUESTED, data=req.model_dump(mode="json"))
    event_ids = await client.send(event)
    event_id = event_ids[0]
    return {
        "event_id": event_id,
        "trace_url": f"{settings.inngest_base_url}/event/{event_id}",
        "lookback_days": req.lookback_days,
        "status": "queued",
    }


@app.get("/digests/latest")
async def get_weekly_digest() -> dict[str, Any]:
    """Fast read (<1s): return the most recent weekly digest from Postgres."""
    async with get_session() as session:
        digest = await get_latest_digest(session)
    if digest is None:
        return {"status": "empty", "digest": None}
    return digest




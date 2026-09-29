"""FastAPI app: serves Inngest functions, plus plain HTTP endpoints for
ingestion and retrieval. Everything is triggered this way, the way
Inngest's own docs describe: send an event, get a handle back, poll for status.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx
import inngest
import inngest.fast_api
from fastapi import FastAPI, HTTPException, Query

from kb.config import settings
from kb.db.queries import get_job_status
from kb.db.session import get_session
from kb.functions import FUNCTIONS
from kb.health import check_health
from kb.inngest_client import client
from kb.retrieval import dense, sparse
from kb.schemas.events import PUBLICATION_ADDED, PublicationAdded

app = FastAPI(title="The Substack Brain")

inngest.fast_api.serve(app, client, FUNCTIONS)


@app.get("/health")
async def health() -> dict[str, Any]:
    return await check_health()


@app.post("/publications")
async def add_publication(payload: PublicationAdded) -> dict[str, Any]:
    """Kick off ingestion for one publication. The feed's host must already be
    listed in `publications.yaml` — `add-publication` rejects anything else.

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


@app.get("/jobs/{event_id}")
async def job_status(event_id: str) -> dict[str, Any]:
    """Combine this repo's own `jobs` row (article counts) with the Inngest Dev
    Server's real run status for the event that started the job.
    """
    trace_url = f"{settings.inngest_base_url}/event/{event_id}"

    run_status: str | None = None
    run_output: object = None
    async with httpx.AsyncClient(timeout=5.0) as http_client:
        try:
            response = await http_client.get(
                f"{settings.inngest_base_url}/v1/events/{event_id}/runs"
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
    mode: Literal["sparse", "dense"] = "sparse",
    limit: int = Query(default=5, ge=1, le=25),
) -> dict[str, Any]:
    """A first, light look at retrieval — sparse (BM25) or dense (pgvector
    cosine similarity), queried directly, no fusion and no LLM synthesis.
    The point is that the two rank the same query differently.
    """
    async with get_session() as session:
        results = await (sparse if mode == "sparse" else dense).search(session, q, limit)
    return {"mode": mode, "results": results}

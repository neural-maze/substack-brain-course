"""kb/article.ingested | kb/article.updated -> extract-claims.

Week 4 durable enrichment pipeline:
1. `load-article`: Fetch Article + Publication metadata and prose from Postgres.
2. `extract-claims-llm`: Run `claim_extractor_agent` via `run_agent` (memoized).
3. `validate-quote-spans`: Deterministic verbatim quote-span guardrail + topic
   canonicalization (`kb/sources/quote_validator.py`).
4. `persist-claims-pg`: Supersede prior active claims on edit and upsert new
   claims into Postgres `claims`.
5. `sync-claims-memgraph`: Idempotently `MERGE` the `:Publication`, `:Author`,
   `:Article`, `:Claim`, `:Topic` subgraph and `[:SUPERSEDES]` edges in Memgraph.
6. `emit-claims-extracted`: Emit `kb/claims.extracted` with `claim_count` and
   `dropped_spans`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

import inngest

from kb.agents.core import run_agent
from kb.agents.roles.extractor import (
    claim_extractor_agent,
    format_extractor_prompt,
    heuristic_extract_claims,
)
from kb.config import settings
from kb.db.queries import get_article_with_publication, persist_article_claims
from kb.db.session import get_session
from kb.graph.client import sync_article_claims_to_graph
from kb.graph.ontology import normalize_author
from kb.inngest_client import client
from kb.schemas.entities import ClaimExtractionBatch
from kb.schemas.events import (
    ARTICLE_INGESTED,
    ARTICLE_UPDATED,
    CLAIMS_EXTRACTED,
    ArticleIngested,
    ArticleUpdated,
    ClaimsExtracted,
)
from kb.sources.quote_validator import validate_and_normalize_claims


@client.create_function(
    fn_id="extract-claims",
    trigger=[
        inngest.TriggerEvent(event=ARTICLE_INGESTED),
        inngest.TriggerEvent(event=ARTICLE_UPDATED),
    ],
    concurrency=[
        # Serialize claim extraction per article so rapid edits never interleave
        inngest.Concurrency(
            key="event.data.canonical_id",
            limit=1,
        ),
        # Global concurrency cap to protect the Anthropic rate limit
        inngest.Concurrency(
            limit=settings.concurrency_extract_claims,
        ),
    ],
    retries=3,
)
async def extract_claims(ctx: inngest.Context) -> dict[str, object]:
    if ctx.event.name == ARTICLE_UPDATED:
        payload: ArticleIngested | ArticleUpdated = ArticleUpdated.model_validate(ctx.event.data)
    else:
        payload = ArticleIngested.model_validate(ctx.event.data)

    async def _load_article() -> dict[str, Any] | None:
        async with get_session() as session:
            row = await get_article_with_publication(session, payload.article_id)
            if row is None:
                return None
            article, pub = row
            if not article.text.strip():
                return None
            return {
                "article_id": str(article.id),
                "canonical_id": article.canonical_id,
                "url": article.url,
                "title": article.title,
                "author": article.author,
                "published_at": article.published_at.isoformat(),
                "content_hash": article.content_hash,
                "text": article.text,
                "pub_slug": pub.slug,
                "pub_name": pub.name,
                "pub_homepage": pub.homepage,
            }

    article_data = await ctx.step.run("load-article", _load_article)
    if not article_data:
        return {
            "article_id": str(payload.article_id),
            "canonical_id": payload.canonical_id,
            "claim_count": 0,
            "dropped_spans": 0,
            "status": "skipped_empty",
        }

    prompt = format_extractor_prompt(
        title=str(article_data["title"]),
        author=cast("str | None", article_data.get("author")),
        publication_name=str(article_data["pub_name"]),
        article_text=str(article_data["text"]),
    )

    if settings.anthropic_api_key:
        extraction_batch = await run_agent(
            step=ctx.step,
            agent=claim_extractor_agent,
            user_input=prompt,
            step_id="extract-claims-llm",
            article_id=payload.article_id,
        )
    else:

        async def _extract_heuristic() -> dict[str, Any]:
            batch = heuristic_extract_claims(str(article_data["text"]))
            return batch.model_dump(mode="json")

        raw_batch = await ctx.step.run("extract-claims-llm", _extract_heuristic)
        extraction_batch = ClaimExtractionBatch.model_validate(raw_batch)

    async def _validate_quote_spans() -> dict[str, Any]:
        validated, dropped = validate_and_normalize_claims(
            article_id=str(article_data["article_id"]),
            content_hash=str(article_data["content_hash"]),
            article_text=str(article_data["text"]),
            raw_claims=extraction_batch.claims,
            fallback_topic=str(article_data["pub_name"]),
        )
        return {
            "claims": [c.model_dump(mode="json") for c in validated],
            "dropped_spans": dropped,
        }

    validation_result = await ctx.step.run("validate-quote-spans", _validate_quote_spans)
    validated_claims = cast("list[dict[str, Any]]", validation_result["claims"])
    dropped_spans = int(cast("int", validation_result["dropped_spans"]))

    async def _persist_claims_pg() -> dict[str, Any]:
        now = datetime.now(UTC)
        async with get_session() as session:
            supersedes_pairs = await persist_article_claims(
                session,
                article_id=uuid.UUID(str(article_data["article_id"])),
                claims=validated_claims,
                now=now,
            )
            await session.commit()
        return {
            "claim_count": len(validated_claims),
            "supersedes_pairs": supersedes_pairs,
            "now_iso": now.isoformat(),
        }

    pg_result = await ctx.step.run("persist-claims-pg", _persist_claims_pg)
    now_iso = str(pg_result["now_iso"])
    supersedes_pairs = cast("list[dict[str, str]]", pg_result["supersedes_pairs"])

    async def _sync_claims_memgraph() -> dict[str, int]:
        author_node = normalize_author(cast("str | None", article_data.get("author")))
        graph_claims = [
            {
                **c,
                "valid_from": now_iso,
                "valid_to": None,
            }
            for c in validated_claims
        ]
        return await sync_article_claims_to_graph(
            pub_slug=str(article_data["pub_slug"]),
            pub_name=str(article_data["pub_name"]),
            pub_homepage=str(article_data["pub_homepage"]),
            author_slug=author_node.slug,
            author_name=author_node.name,
            canonical_id=str(article_data["canonical_id"]),
            article_id=str(article_data["article_id"]),
            url=str(article_data["url"]),
            title=str(article_data["title"]),
            published_at=str(article_data["published_at"]),
            content_hash=str(article_data["content_hash"]),
            claims=graph_claims,
            supersedes_pairs=supersedes_pairs,
            now_iso=now_iso,
        )

    await ctx.step.run("sync-claims-memgraph", _sync_claims_memgraph)

    async def _emit_claims_extracted() -> dict[str, bool]:
        event_payload = ClaimsExtracted(
            article_id=uuid.UUID(str(article_data["article_id"])),
            canonical_id=str(article_data["canonical_id"]),
            claim_count=len(validated_claims),
            dropped_spans=dropped_spans,
        )
        await client.send(
            inngest.Event(
                name=CLAIMS_EXTRACTED,
                data=event_payload.model_dump(mode="json"),
            )
        )
        return {"event_sent": True}

    await ctx.step.run("emit-claims-extracted", _emit_claims_extracted)

    return {
        "article_id": str(article_data["article_id"]),
        "canonical_id": str(article_data["canonical_id"]),
        "claim_count": len(validated_claims),
        "dropped_spans": dropped_spans,
    }

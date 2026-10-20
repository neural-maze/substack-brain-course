"""kb/article.discovered -> ingest-article.

Each step below is retried and memoized independently — the heart of the
week-1 lesson: a failure in `embed` re-runs only `embed`, never re-fetching
or re-parsing the article. Re-sending the same event produces zero new rows
(upserts throughout: `articles` by `canonical_id`, `passages` by
`(article_id, chunk_index)`), so retrying a failed article is always safe.

Week 2 adds flow control, configured on the function itself so Inngest
enforces it in its queue, before any of our code runs:

- concurrency, keyed by publication: at most N articles per website at once;
- throttle, global: at most N runs START per minute, to protect the OpenAI quota;
- priority: manual submissions (`POST /articles`) jump ahead of backfill runs.

There is deliberately no Inngest `idempotency` key. It would block re-sending
the same article for 24h, which breaks week 1's "re-add the publication to
retry the articles that failed". Duplicates are already prevented by the
backfill's "skip what's stored" filter and by the upserts above.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import cast

import inngest

from kb.config import settings
from kb.db.queries import (
    get_active_claims_for_article,
    get_article_by_canonical_id,
    insert_passages_fresh,
    replace_passages,
    supersede_passages,
    upsert_article,
)
from kb.db.session import get_session
from kb.inngest_client import client
from kb.schemas.events import (
    ARTICLE_DISCOVERED,
    ARTICLE_INGESTED,
    ARTICLE_UPDATED,
    ArticleDiscovered,
    ArticleIngested,
    ArticleUpdated,
)
from kb.sources.allowlist import assert_allowed, load_publications
from kb.sources.chunking import chunk_text
from kb.sources.embedder import embed_texts
from kb.sources.fetcher import fetch_article
from kb.sources.parser import parse_html


@client.create_function(
    fn_id="ingest-article",
    trigger=inngest.TriggerEvent(event=ARTICLE_DISCOVERED),
    # A separate limit for each publication, so we never hit one website with
    # more than N requests at once, and one big backfill can't starve the others.
    concurrency=[
        inngest.Concurrency(
            key="event.data.publication_id",
            limit=settings.concurrency_per_publication,
        )
    ],
    # No key: the OpenAI quota is shared by every publication, so the limit is too.
    # Throttling limits run STARTS, not steps; embed_texts sends a whole article
    # in one request, which keeps "runs per minute" close to "requests per minute".
    throttle=inngest.Throttle(
        limit=settings.ingest_runs_per_minute,
        period=timedelta(minutes=1),
    ),
    # Priority is a time shift in seconds, not a rank: +600 schedules a manual run
    # as if it had arrived 10 minutes earlier, and -600 pushes backfill runs 10
    # minutes later, so a manual article gets up to 20 minutes of head start.
    priority=inngest.Priority(
        run=(
            f"event.data.via == 'manual' ? {settings.priority_manual_s} "
            f": {settings.priority_backfill_s}"
        )
    ),
    retries=3,
)
async def ingest_article(ctx: inngest.Context) -> dict[str, object]:
    payload = ArticleDiscovered.model_validate(ctx.event.data)

    async def _check_allowlist() -> None:
        try:
            assert_allowed(payload.url, load_publications())
        except ValueError as exc:
            raise inngest.NonRetriableError(str(exc)) from exc

    await ctx.step.run("check-allowlist", _check_allowlist)

    async def _fetch_html() -> dict[str, object]:
        page = await fetch_article(payload.url)
        return {"url": page.url, "html": page.html, "status_code": page.status_code}

    page = await ctx.step.run("fetch-html", _fetch_html)

    async def _parse() -> dict[str, object]:
        article = parse_html(
            str(page["html"]), str(page["url"]), fallback_published_at=payload.published_at
        )
        return article.model_dump(mode="json")

    parsed = await ctx.step.run("parse", _parse)

    async def _check_content_change() -> dict[str, object]:
        async with get_session() as session:
            existing = await get_article_by_canonical_id(session, payload.canonical_id)
            if existing is None:
                return {"is_new": True, "changed": True, "article_id": None}
            if existing.content_hash == str(parsed["content_hash"]):
                active_claims = await get_active_claims_for_article(session, existing.id)
                return {
                    "is_new": False,
                    "changed": False,
                    "article_id": str(existing.id),
                    "needs_claims": len(active_claims) == 0,
                }
            return {"is_new": False, "changed": True, "article_id": str(existing.id)}

    change_info = await ctx.step.run("check-content-change", _check_content_change)

    if not change_info["changed"]:
        if change_info.get("needs_claims"):

            async def _emit_for_enrichment() -> dict[str, bool]:
                ingested_event = ArticleIngested(
                    canonical_id=payload.canonical_id,
                    article_id=change_info["article_id"],  # type: ignore[arg-type]
                    content_hash=str(parsed["content_hash"]),
                    passage_count=0,
                )
                await client.send(
                    inngest.Event(
                        name=ARTICLE_INGESTED, data=ingested_event.model_dump(mode="json")
                    )
                )
                return {"event_sent": True}

            await ctx.step.run("emit-for-enrichment", _emit_for_enrichment)
        return {
            "article_id": str(change_info["article_id"]),
            "passage_count": 0,
            "status": "unchanged",
        }

    is_update = not bool(change_info["is_new"])

    async def _chunk() -> list[str]:
        return chunk_text(
            str(parsed["text"]),
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
        )

    chunks = await ctx.step.run("chunk", _chunk)

    async def _embed() -> dict[str, object]:
        return await embed_texts(chunks)

    embed_result = await ctx.step.run("embed", _embed)

    async def _persist() -> dict[str, object]:
        embeddings = cast("list[list[float]]", embed_result["embeddings"])
        async with get_session() as session:
            article_id = await upsert_article(
                session,
                canonical_id=payload.canonical_id,
                publication_id=payload.publication_id,
                url=str(page["url"]),
                title=str(parsed["title"]),
                author=parsed["author"],  # type: ignore[arg-type]
                published_at=datetime.fromisoformat(str(parsed["published_at"])),
                content_hash=str(parsed["content_hash"]),
                text=str(parsed["text"]),
            )
            passages_data = [
                {"chunk_index": i, "text": chunk, "embedding": embedding}
                for i, (chunk, embedding) in enumerate(zip(chunks, embeddings, strict=True))
            ]
            if is_update:
                await supersede_passages(session, article_id=article_id)
                passage_count = await insert_passages_fresh(
                    session, article_id=article_id, passages=passages_data
                )
            else:
                passage_count = await replace_passages(
                    session, article_id=article_id, passages=passages_data
                )
            await session.commit()
            return {"article_id": str(article_id), "passage_count": passage_count}

    persisted = await ctx.step.run("persist", _persist)

    async def _emit() -> dict[str, bool]:
        if is_update:
            updated_event = ArticleUpdated(
                canonical_id=payload.canonical_id,
                article_id=persisted["article_id"],  # type: ignore[arg-type]
                content_hash=str(parsed["content_hash"]),
                passage_count=persisted["passage_count"],  # type: ignore[arg-type]
            )
            await client.send(
                inngest.Event(name=ARTICLE_UPDATED, data=updated_event.model_dump(mode="json"))
            )
        else:
            ingested_event = ArticleIngested(
                canonical_id=payload.canonical_id,
                article_id=persisted["article_id"],  # type: ignore[arg-type]
                content_hash=str(parsed["content_hash"]),
                passage_count=persisted["passage_count"],  # type: ignore[arg-type]
            )
            await client.send(
                inngest.Event(name=ARTICLE_INGESTED, data=ingested_event.model_dump(mode="json"))
            )
        return {"event_sent": True}

    await ctx.step.run("emit", _emit)

    return persisted

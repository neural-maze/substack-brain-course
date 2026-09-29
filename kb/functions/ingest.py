"""kb/article.discovered -> ingest-article.

Each step below is retried and memoized independently — the heart of the
week-1 lesson: a failure in `embed` re-runs only `embed`, never re-fetching
or re-parsing the article. Re-sending the same event produces zero new rows
(upserts throughout: `articles` by `canonical_id`, `passages` by
`(article_id, chunk_index)`).
"""

from __future__ import annotations

from datetime import datetime

import inngest

from kb.config import settings
from kb.db.queries import replace_passages, upsert_article
from kb.db.session import get_session
from kb.inngest_client import client
from kb.schemas.events import (
    ARTICLE_DISCOVERED,
    ARTICLE_INGESTED,
    ArticleDiscovered,
    ArticleIngested,
)
from kb.sources.allowlist import assert_allowed, load_publications
from kb.sources.chunking import chunk_text
from kb.sources.embedder import embed_texts
from kb.sources.fetcher import fetch_article
from kb.sources.parser import parse_html


@client.create_function(
    fn_id="ingest-article",
    trigger=inngest.TriggerEvent(event=ARTICLE_DISCOVERED),
    concurrency=[inngest.Concurrency(key="event.data.publication_id", limit=3)],
    idempotency="event.data.canonical_id",
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
        embeddings = embed_result["embeddings"]
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
            passage_count = await replace_passages(
                session,
                article_id=article_id,
                passages=[
                    {"chunk_index": i, "text": chunk, "embedding": embedding}
                    for i, (chunk, embedding) in enumerate(zip(chunks, embeddings, strict=True))
                ],
            )
            await session.commit()
            return {"article_id": str(article_id), "passage_count": passage_count}

    persisted = await ctx.step.run("persist", _persist)

    async def _emit() -> dict[str, bool]:
        event = ArticleIngested(
            canonical_id=payload.canonical_id,
            article_id=persisted["article_id"],  # type: ignore[arg-type]
            content_hash=str(parsed["content_hash"]),
            passage_count=persisted["passage_count"],  # type: ignore[arg-type]
        )
        await client.send(inngest.Event(name=ARTICLE_INGESTED, data=event.model_dump(mode="json")))
        return {"event_sent": True}

    await ctx.step.run("emit", _emit)

    return persisted

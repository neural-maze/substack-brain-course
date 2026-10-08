"""kb/publication.added -> backfill-publication.

Week 2's replacement for `add-publication`. Same first three steps (allowlist,
feed, publication row), but instead of the latest `discovery_limit` entries it
takes EVERY entry in the feed, skips the articles already stored, and fans out
`kb/article.discovered` with `via="backfill"` in batches.

Note that a Substack RSS feed only carries a publication's most recent posts
(around 20), so "every entry in the feed" is not the full archive.

The fan-out is cheap on purpose: the expensive work happens in
`ingest-article`, where concurrency, throttling and priority decide when each
article actually runs.
"""

from __future__ import annotations

import uuid
from typing import Any

import inngest

from kb.config import settings
from kb.db.queries import create_job, get_existing_canonical_ids, upsert_publication
from kb.db.session import get_session
from kb.inngest_client import client
from kb.schemas.events import (
    ARTICLE_DISCOVERED,
    PUBLICATION_ADDED,
    ArticleDiscovered,
    PublicationAdded,
)
from kb.schemas.ids import canonical_id
from kb.sources.allowlist import PublicationEntry, find_publication_for_url, load_publications
from kb.sources.rss import fetch_feed, parse_feed


@client.create_function(
    fn_id="backfill-publication",
    trigger=inngest.TriggerEvent(event=PUBLICATION_ADDED),
    retries=3,
)
async def backfill_publication(ctx: inngest.Context) -> dict[str, int]:
    payload = PublicationAdded.model_validate(ctx.event.data)

    async def _match_allowlist() -> dict[str, object]:
        pub = find_publication_for_url(payload.feed_url, load_publications())
        if pub is None:
            raise inngest.NonRetriableError(
                f"Feed is outside the allowlist (publications.yaml): {payload.feed_url!r}"
            )
        return pub.model_dump()

    matched = await ctx.step.run("match-allowlist", _match_allowlist)
    pub = PublicationEntry.model_validate(matched)

    async def _fetch_feed() -> str:
        raw = await fetch_feed(pub.feed_url)
        return raw.decode("utf-8", errors="replace")

    feed_text = await ctx.step.run("fetch-feed", _fetch_feed)

    async def _upsert_publication() -> str:
        async with get_session() as session:
            publication_id = await upsert_publication(
                session,
                slug=pub.slug,
                name=pub.name,
                feed_url=pub.feed_url,
                homepage=pub.homepage,
            )
            await session.commit()
            return str(publication_id)

    publication_id = uuid.UUID(await ctx.step.run("upsert-publication", _upsert_publication))

    async def _find_new_articles() -> list[dict[str, Any]]:
        candidates = [
            ArticleDiscovered(
                canonical_id=canonical_id(pub.slug, entry.url),
                publication_id=publication_id,
                url=entry.url,
                title=entry.title,
                published_at=entry.published_at,
                via="backfill",
            )
            for entry in parse_feed(feed_text.encode("utf-8"))
        ]
        if not candidates:
            return []
        async with get_session() as session:
            stored = await get_existing_canonical_ids(session, [c.canonical_id for c in candidates])
        return [c.model_dump(mode="json") for c in candidates if c.canonical_id not in stored]

    new_articles = await ctx.step.run("find-new-articles", _find_new_articles)

    async def _record_and_fan_out() -> dict[str, int]:
        # The job row goes first: GET /jobs counts articles written after the job
        # started, so it must exist before the first article can land.
        async with get_session() as session:
            await create_job(
                session,
                event_id=ctx.event.id,
                kind="backfill",
                total=len(new_articles),
                publication_id=publication_id,
            )
            await session.commit()

        batch_size = settings.backfill_batch_size
        for start in range(0, len(new_articles), batch_size):
            batch = new_articles[start : start + batch_size]
            await client.send([inngest.Event(name=ARTICLE_DISCOVERED, data=d) for d in batch])
        return {"discovered": len(new_articles)}

    return await ctx.step.run("record-and-fan-out", _record_and_fan_out)

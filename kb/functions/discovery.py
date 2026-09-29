"""kb/publication.added -> discovery.

Fetches the feed, upserts the publication, takes the most recent
`discovery_limit` entries, and fans out
`kb/article.discovered` with `via="rss"` for every entry that isn't stored yet.
Re-adding a publication therefore retries exactly the articles whose
`ingest-article` run failed, and skips the ones that already succeeded.
"""

from __future__ import annotations

import uuid

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
    fn_id="add-publication",
    trigger=inngest.TriggerEvent(event=PUBLICATION_ADDED),
    retries=3,
)
async def add_publication(ctx: inngest.Context) -> dict[str, int]:
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

    publication_id = await ctx.step.run("upsert-publication", _upsert_publication)

    async def _discover_and_emit() -> dict[str, int]:
        entries = parse_feed(feed_text.encode("utf-8"))[: settings.discovery_limit]
        discovered = [
            ArticleDiscovered(
                canonical_id=canonical_id(pub.slug, entry.url),
                publication_id=publication_id,  # pydantic coerces the UUID string
                url=entry.url,
                title=entry.title,
                published_at=entry.published_at,
                via="rss",
            )
            for entry in entries
        ]
        async with get_session() as session:
            stored = await get_existing_canonical_ids(session, [d.canonical_id for d in discovered])
        missing = [d for d in discovered if d.canonical_id not in stored]
        if missing:
            # One batched send, not one call per article.
            await client.send(
                [
                    inngest.Event(name=ARTICLE_DISCOVERED, data=d.model_dump(mode="json"))
                    for d in missing
                ]
            )
        async with get_session() as session:
            await create_job(
                session,
                event_id=ctx.event.id,
                kind="discovery",
                total=len(discovered),
                publication_id=uuid.UUID(publication_id),
            )
            await session.commit()
        return {"count": len(discovered), "emitted": len(missing)}

    return await ctx.step.run("discover-and-emit", _discover_and_emit)

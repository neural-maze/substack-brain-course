"""Freshness: conditional polling cron.

Runs every 5 minutes. Sends If-None-Match / If-Modified-Since headers to
each active publication origin.
- 304 (Not Modified): Zero downstream work. Increment not_modified_count.
- 200 (Modified): Parse entries, filter out known canonical IDs, and emit
  `kb/article.discovered` only for genuinely new content.
"""

from __future__ import annotations

import inngest

from kb.db.queries import (
    get_cursor,
    get_existing_canonical_ids,
    get_publication_by_slug,
    update_cursor,
    upsert_publication,
)
from kb.db.session import get_session
from kb.inngest_client import client
from kb.schemas.events import ARTICLE_DISCOVERED, ArticleDiscovered
from kb.schemas.ids import canonical_id
from kb.sources.allowlist import load_publications
from kb.sources.rss import fetch_feed_conditional, parse_feed


@client.create_function(
    fn_id="poll-feeds",
    name="Poll Feeds for Freshness",
    trigger=[
        inngest.TriggerCron(cron="*/5 * * * *"),
        inngest.TriggerEvent(event="kb/feed.poll"),
    ],
)
async def poll_feeds(ctx: inngest.Context) -> dict[str, object]:
    """Poll active publication feeds conditionally."""
    pubs = load_publications()
    results: list[dict[str, object]] = []

    for pub_meta in pubs:
        async def _poll_one(
            meta: object = pub_meta,
            slug: str = pub_meta.slug,
            feed_url: str = pub_meta.feed_url,
        ) -> dict[str, object]:
            async with get_session() as session:
                pub = await get_publication_by_slug(session, slug)
                if pub is None:
                    pub_id = await upsert_publication(
                        session,
                        slug=slug,
                        name=getattr(meta, "name", slug),
                        feed_url=feed_url,
                        homepage=getattr(meta, "homepage", ""),
                    )
                else:
                    pub_id = pub.id

                cursor = await get_cursor(session, pub_id)
                etag = cursor.etag if cursor else None
                last_modified = cursor.last_modified if cursor else None

                status_code, raw, resp_etag, resp_last_modified = await fetch_feed_conditional(
                    feed_url, etag=etag, last_modified=last_modified
                )

                if status_code == 304 or raw is None:
                    await update_cursor(
                        session,
                        publication_id=pub_id,
                        etag=resp_etag,
                        last_modified=resp_last_modified,
                        not_modified=True,
                    )
                    await session.commit()
                    return {"slug": slug, "status": 304, "new_articles": 0}

                # Status 200: Parse and check for new content
                await update_cursor(
                    session,
                    publication_id=pub_id,
                    etag=resp_etag,
                    last_modified=resp_last_modified,
                    not_modified=False,
                )
                await session.commit()

                entries = parse_feed(raw)
                if not entries:
                    return {"slug": slug, "status": 200, "new_articles": 0}

                entry_map = {canonical_id(slug, e.url): e for e in entries}
                existing_ids = await get_existing_canonical_ids(session, list(entry_map.keys()))
                new_ids = [cid for cid in entry_map if cid not in existing_ids]

                events: list[inngest.Event] = []
                for cid in new_ids:
                    entry = entry_map[cid]
                    events.append(
                        inngest.Event(
                            name=ARTICLE_DISCOVERED,
                            data=ArticleDiscovered(
                                canonical_id=cid,
                                publication_id=pub_id,
                                url=entry.url,
                                title=entry.title,
                                published_at=entry.published_at,
                                via="poll",
                            ).model_dump(mode="json"),
                        )
                    )

                if events:
                    await client.send(events)

                return {"slug": slug, "status": 200, "new_articles": len(events)}

        res = await ctx.step.run(f"poll-{pub_meta.slug}", _poll_one)
        results.append(res)

    return {"polled_count": len(results), "results": results}

"""RSS is an open standard, not a Substack feature — nothing here assumes Substack."""

from __future__ import annotations

import calendar
from datetime import UTC, datetime

import feedparser
import httpx
from pydantic import BaseModel

from kb.config import settings


class FeedEntry(BaseModel):
    title: str
    url: str
    published_at: datetime


async def fetch_feed(feed_url: str) -> bytes:
    async with httpx.AsyncClient(
        headers={"User-Agent": settings.user_agent}, timeout=settings.request_timeout_s
    ) as client:
        response = await client.get(feed_url, follow_redirects=True)
        response.raise_for_status()
        return response.content


def parse_feed(raw: bytes) -> list[FeedEntry]:
    """Fail clearly if the feed does not parse at all."""
    parsed = feedparser.parse(raw)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"Feed did not parse: {parsed.get('bozo_exception')}")

    entries: list[FeedEntry] = []
    for entry in parsed.entries:
        url = entry.get("link")
        title = entry.get("title")
        published_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        if not url or not title or not published_parsed:
            continue  # skip entries too malformed to use, don't fail the whole feed
        published_at = datetime.fromtimestamp(calendar.timegm(published_parsed), tz=UTC)
        entries.append(FeedEntry(title=title, url=url, published_at=published_at))
    return entries

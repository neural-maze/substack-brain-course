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


async def fetch_feed_conditional(
    feed_url: str,
    etag: str | None = None,
    last_modified: str | None = None,
) -> tuple[int, bytes | None, str | None, str | None]:
    """Fetch an RSS feed conditionally using ETag and Last-Modified headers.
    Returns (status_code, content_or_none, response_etag, response_last_modified).
    """
    headers: dict[str, str] = {"User-Agent": settings.user_agent}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    async with httpx.AsyncClient(headers=headers, timeout=settings.request_timeout_s) as client:
        response = await client.get(feed_url, follow_redirects=True)
        resp_etag = response.headers.get("ETag") or etag
        resp_last_modified = response.headers.get("Last-Modified") or last_modified

        if response.status_code == 304:
            return 304, None, resp_etag, resp_last_modified

        response.raise_for_status()
        return response.status_code, response.content, resp_etag, resp_last_modified



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

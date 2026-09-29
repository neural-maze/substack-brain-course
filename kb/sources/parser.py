"""HTML -> clean text + metadata.

trafilatura strips boilerplate (subscribe prompts, share buttons, footers)
far more reliably than a hand-rolled selector list — validated against a
recorded real Neural Maze article, not a hand-written fixture.
"""

from __future__ import annotations

import contextlib
import hashlib
from datetime import UTC, datetime

import trafilatura
from pydantic import BaseModel


class ParsedArticle(BaseModel):
    title: str
    author: str | None
    published_at: datetime
    text: str
    content_hash: str


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_html(html: str, url: str, *, fallback_published_at: datetime) -> ParsedArticle:
    """Extract title/author/date/body from a fetched article page.

    `fallback_published_at` (the RSS feed's `pubDate`) is used only when the
    page itself carries no extractable date. HTML metadata is preferred
    because it reflects the article as actually published, not the feed
    entry that triggered discovery.
    """
    text = trafilatura.extract(
        html,
        url=url,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
    )
    if not text:
        raise ValueError(f"trafilatura could not extract any content from {url!r}")

    metadata = trafilatura.extract_metadata(html, default_url=url)
    title = (metadata.title if metadata else None) or url
    author = metadata.author if metadata else None

    published_at = fallback_published_at
    if metadata and metadata.date:
        with contextlib.suppress(ValueError):
            published_at = datetime.fromisoformat(metadata.date).replace(tzinfo=UTC)

    return ParsedArticle(
        title=title,
        author=author,
        published_at=published_at,
        text=text,
        content_hash=_hash_text(text),
    )

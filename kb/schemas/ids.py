"""ID helpers — the dedupe keys for the whole project.

Pure functions, no I/O: they must be unit-testable without a database or
network, and every writer in the pipeline (RSS, backfill, freshness,
manual ingestion) calls through them instead of building ids ad hoc.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def normalize_url(url: str) -> str:
    """Canonicalize a URL for dedup.

    Forces https, lowercases the host, drops the port, query string and
    fragment, and strips trailing slashes from the path. This is what lets
    utm links, `?ref=` share links, http vs https and trailing-slash
    variants of the same post collapse to one URL.
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, "", ""))


def canonical_id(publication_slug: str, url: str) -> str:
    """`pub:<publication_slug>:<post_slug>` — the cross-pipeline dedupe key.

    `publication_slug` is passed in rather than derived from the URL's host:
    every producer (discovery, backfill, freshness, manual add) already
    knows which publication it is working from the allowlist, so this stays
    a pure string function instead of doing a lookup.
    """
    normalized = normalize_url(url)
    segments = [s for s in urlsplit(normalized).path.split("/") if s]
    if not segments:
        raise ValueError(f"URL has no path segment to use as a post slug: {url!r}")
    post_slug = segments[-1]
    return f"pub:{publication_slug}:{post_slug}"


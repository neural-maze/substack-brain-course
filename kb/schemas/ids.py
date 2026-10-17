"""ID helpers — the dedupe keys for the whole project.

Pure functions, no I/O: they must be unit-testable without a database or
network, and every writer in the pipeline (RSS, backfill, freshness,
manual ingestion) calls through them instead of building ids ad hoc.
"""

from __future__ import annotations

import secrets
import uuid
from urllib.parse import urlsplit, urlunsplit

ARTICLE_CLAIM_NAMESPACE = uuid.UUID("7b9e4c1a-3d2f-4e8b-9a1c-5f6d7e8a9b0c")


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


def snapshot_id() -> str:
    """`snap_<12 url-safe chars>`: the id of one answer produced by `POST /ask`.

    It joins an answer with everything recorded about it later (scores, feedback).
    """
    return f"snap_{secrets.token_urlsafe(9)}"


def claim_id(
    article_id: uuid.UUID | str,
    content_hash: str,
    quote_span: str,
    claim_text: str,
) -> uuid.UUID:
    """Deterministic UUIDv5 for a claim extracted from a specific article revision.

    Re-running `extract-claims` on the same article revision converges on the
    exact same UUID in both Postgres and Memgraph.
    """
    norm_quote = " ".join(quote_span.split()).strip().lower()
    norm_claim = " ".join(claim_text.split()).strip().lower()
    seed = f"{article_id}:{content_hash}:{norm_quote}:{norm_claim}"
    return uuid.uuid5(ARTICLE_CLAIM_NAMESPACE, seed)


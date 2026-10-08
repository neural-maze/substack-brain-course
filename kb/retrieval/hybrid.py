"""Hybrid retrieval: full-text search + vector search, fused with Reciprocal Rank Fusion.

Neither engine is enough alone. Full-text search (`sparse.py`, Postgres
`tsvector` + `ts_rank`) nails exact tokens like function names and version
strings but misses synonyms. Vector search (`dense.py`, pgvector) understands
meaning but blurs exact tokens. RRF merges both rankings using only each
passage's POSITION in each list, so the two engines' scores, which live on
different scales, are never added together.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from kb.config import settings
from kb.db.queries import search_passages_bm25, search_passages_dense
from kb.sources.embedder import embed_texts


@dataclass
class RetrievedCandidate:
    passage_id: str
    text: str
    title: str
    author: str | None
    url: str
    published_at: str
    rrf_score: float
    sparse_rank: int | None = None
    sparse_score: float | None = None
    dense_rank: int | None = None
    dense_score: float | None = None
    # "bm25" (full-text only), "vector" (vector only) or "both".
    source: str = "bm25"


def _candidate(item: dict[str, Any], rrf_score: float) -> RetrievedCandidate:
    return RetrievedCandidate(
        passage_id=str(item["passage_id"]),
        text=str(item["text"]),
        title=str(item["title"]),
        author=item.get("author"),
        url=str(item["url"]),
        published_at=str(item["published_at"]),
        rrf_score=rrf_score,
    )


def compute_rrf(
    sparse_results: list[dict[str, Any]],
    dense_results: list[dict[str, Any]],
    k: int = 60,
) -> list[RetrievedCandidate]:
    """score(passage) = sum over engines of 1 / (k + rank in that engine).

    A passage found by both engines collects points twice, so agreement between
    two very different engines beats being one engine's favourite. Ties are
    broken by passage_id, so the same question always gives the same order.
    """
    candidates: dict[str, RetrievedCandidate] = {}

    for rank, item in enumerate(sparse_results, start=1):
        cand = _candidate(item, 1.0 / (k + rank))
        cand.sparse_rank = rank
        cand.sparse_score = float(item.get("rank", 0.0))
        cand.source = "bm25"
        candidates[cand.passage_id] = cand

    for rank, item in enumerate(dense_results, start=1):
        pid = str(item["passage_id"])
        if pid in candidates:
            cand = candidates[pid]
            cand.rrf_score += 1.0 / (k + rank)
            cand.source = "both"
        else:
            cand = _candidate(item, 1.0 / (k + rank))
            cand.source = "vector"
            candidates[pid] = cand
        cand.dense_rank = rank
        cand.dense_score = float(item.get("rank", 0.0))

    return sorted(candidates.values(), key=lambda c: (-c.rrf_score, c.passage_id))


async def hybrid_search(
    session: AsyncSession,
    query: str,
    limit: int = 5,
    fetch_multiplier: int = 3,
    k: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (top `limit` passages for the prompt, every fused candidate for the snapshot)."""
    rrf_k = k or settings.rrf_k
    candidate_fetch_limit = max(limit * fetch_multiplier, 15)

    # One AsyncSession cannot run two queries at the same time. So we overlap the
    # slow network call (embedding the question with OpenAI) with the full-text
    # query, and only run the vector query once the embedding is back.
    sparse_results, embedded = await asyncio.gather(
        search_passages_bm25(session, query, candidate_fetch_limit),
        embed_texts([query]),
    )
    (query_embedding,) = embedded["embeddings"]
    dense_results = await search_passages_dense(session, query_embedding, candidate_fetch_limit)

    fused = [asdict(c) for c in compute_rrf(sparse_results, dense_results, k=rrf_k)]
    return fused[:limit], fused


async def search(session: AsyncSession, query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Same interface as `sparse.search` and `dense.search`, used by `GET /search`."""
    top, _ = await hybrid_search(session, query, limit=limit)
    return top

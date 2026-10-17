"""Three-way hybrid retrieval: BM25 + pgvector + Memgraph claim traversal,
fused with Reciprocal Rank Fusion.

Neither engine is enough alone:
- Full-text search (`sparse.py`, Postgres `tsvector` + `ts_rank`) nails exact tokens.
- Vector search (`dense.py`, pgvector) captures semantic similarity.
- Graph traversal (`graph.py`, Memgraph) bridges attributed claims across
  publications through shared `:Topic` nodes, with no natural similarity score.

Because RRF depends only on each candidate's ordinal POSITION ($1, 2, \\dots, N$)
in each ranking, all three engines fuse cleanly without score calibration.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from kb.config import settings
from kb.db.queries import search_passages_bm25, search_passages_dense
from kb.retrieval.graph import search_graph_claims
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
    graph_rank: int | None = None
    claim_id: str | None = None
    claim_text: str | None = None
    claim_type: str | None = None
    # "bm25", "vector", "both", "graph", "bm25+graph", "vector+graph", or "all"
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
        claim_id=str(item["claim_id"]) if item.get("claim_id") else None,
        claim_text=str(item["claim_text"]) if item.get("claim_text") else None,
        claim_type=str(item["claim_type"]) if item.get("claim_type") else None,
    )


def _format_source(has_bm25: bool, has_vector: bool, has_graph: bool) -> str:
    if has_bm25 and has_vector and has_graph:
        return "all"
    if has_bm25 and has_vector:
        return "both"
    if has_bm25 and has_graph:
        return "bm25+graph"
    if has_vector and has_graph:
        return "vector+graph"
    if has_graph:
        return "graph"
    if has_vector:
        return "vector"
    return "bm25"


def compute_rrf(
    sparse_results: list[dict[str, Any]],
    dense_results: list[dict[str, Any]],
    graph_results: list[dict[str, Any]] | None = None,
    k: int = 60,
) -> list[RetrievedCandidate]:
    """score(passage) = sum over engines of 1 / (k + rank in that engine).

    A passage found by multiple engines collects points from each. Ties are
    broken by `passage_id`, so the same question always gives the same order.
    """
    candidates: dict[str, RetrievedCandidate] = {}

    for rank, item in enumerate(sparse_results, start=1):
        pid = str(item["passage_id"])
        if pid in candidates:
            continue
        cand = _candidate(item, 1.0 / (k + rank))
        cand.sparse_rank = rank
        cand.sparse_score = float(item.get("rank", 0.0))
        cand.source = "bm25"
        candidates[pid] = cand

    seen_dense: set[str] = set()
    for rank, item in enumerate(dense_results, start=1):
        pid = str(item["passage_id"])
        if pid in seen_dense:
            continue
        seen_dense.add(pid)
        if pid in candidates:
            cand = candidates[pid]
            cand.rrf_score += 1.0 / (k + rank)
        else:
            cand = _candidate(item, 1.0 / (k + rank))
            candidates[pid] = cand
        cand.dense_rank = rank
        cand.dense_score = float(item.get("rank", 0.0))
        cand.source = _format_source(
            cand.sparse_rank is not None,
            cand.dense_rank is not None,
            cand.graph_rank is not None,
        )

    seen_graph: set[str] = set()
    for rank, item in enumerate(graph_results or [], start=1):
        pid = str(item["passage_id"])
        if pid in seen_graph:
            continue
        seen_graph.add(pid)
        if pid in candidates:
            cand = candidates[pid]
            cand.rrf_score += 1.0 / (k + rank)
            if cand.claim_id is None and item.get("claim_id"):
                cand.claim_id = str(item["claim_id"])
                cand.claim_text = str(item.get("claim_text", ""))
                cand.claim_type = str(item.get("claim_type", ""))
        else:
            cand = _candidate(item, 1.0 / (k + rank))
            candidates[pid] = cand
        cand.graph_rank = rank
        cand.source = _format_source(
            cand.sparse_rank is not None,
            cand.dense_rank is not None,
            cand.graph_rank is not None,
        )

    return sorted(candidates.values(), key=lambda c: (-c.rrf_score, c.passage_id))


async def hybrid_search(
    session: AsyncSession,
    query: str,
    limit: int = 5,
    fetch_multiplier: int = 3,
    k: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return `(top limit passages for the prompt, every fused candidate for the snapshot)`."""
    rrf_k = k or settings.rrf_k
    candidate_fetch_limit = max(limit * fetch_multiplier, 15)

    # One AsyncSession cannot run two SQL queries at the same time. So we overlap
    # the network call (embedding the question with OpenAI) with the full-text
    # query, and run the vector and graph queries sequentially on the session.
    sparse_results, embedded = await asyncio.gather(
        search_passages_bm25(session, query, candidate_fetch_limit),
        embed_texts([query]),
    )
    (query_embedding,) = embedded["embeddings"]
    dense_results = await search_passages_dense(session, query_embedding, candidate_fetch_limit)
    graph_results = await search_graph_claims(
        session, query, limit=settings.graph_retrieval_limit
    )

    fused = [
        asdict(c)
        for c in compute_rrf(sparse_results, dense_results, graph_results, k=rrf_k)
    ]
    return fused[:limit], fused


async def search(session: AsyncSession, query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Same interface as `sparse.search` and `dense.search`, used by `GET /search`."""
    top, _ = await hybrid_search(session, query, limit=limit)
    return top

"""Unit tests for three-way Reciprocal Rank Fusion (`kb/retrieval/hybrid.py` & `graph.py`)."""

from __future__ import annotations

from kb.retrieval.graph import extract_query_seed_topics
from kb.retrieval.hybrid import compute_rrf


def _candidate_dict(
    pid: str,
    *,
    claim_id: str | None = None,
    claim_text: str | None = None,
    claim_type: str | None = None,
) -> dict[str, object]:
    item: dict[str, object] = {
        "passage_id": pid,
        "text": f"passage text for {pid}",
        "title": f"Article {pid}",
        "author": "Test Author",
        "url": f"https://example.com/p/{pid}",
        "published_at": "2026-10-15T00:00:00+00:00",
        "rank": 0.85,
    }
    if claim_id:
        item["claim_id"] = claim_id
        item["claim_text"] = claim_text or f"Claim in {pid}"
        item["claim_type"] = claim_type or "opinion"
    return item


def test_three_way_rrf_fuses_bm25_vector_and_graph_rankings() -> None:
    sparse = [_candidate_dict("P1"), _candidate_dict("P2")]
    dense = [_candidate_dict("P2"), _candidate_dict("P3")]
    graph = [
        _candidate_dict(
            "P4", claim_id="C4", claim_text="Graph-only claim", claim_type="measurement"
        ),
        _candidate_dict(
            "P2", claim_id="C2", claim_text="All-three claim", claim_type="opinion"
        ),
        _candidate_dict(
            "P1", claim_id="C1", claim_text="BM25+Graph claim", claim_type="prediction"
        ),
    ]

    fused = compute_rrf(sparse, dense, graph, k=60)
    by_id = {c.passage_id: c for c in fused}

    # P2 appears in all 3 engines (rank 2 in sparse, rank 1 in dense, rank 2 in graph)
    assert by_id["P2"].source == "all"
    assert (by_id["P2"].sparse_rank, by_id["P2"].dense_rank, by_id["P2"].graph_rank) == (2, 1, 2)
    assert abs(by_id["P2"].rrf_score - (1 / 62 + 1 / 61 + 1 / 62)) < 1e-12
    assert by_id["P2"].claim_id == "C2"

    # P1 appears in sparse (#1) and graph (#3)
    assert by_id["P1"].source == "bm25+graph"
    assert (by_id["P1"].sparse_rank, by_id["P1"].dense_rank, by_id["P1"].graph_rank) == (1, None, 3)

    # P4 appears only in graph (#1)
    assert by_id["P4"].source == "graph"
    assert by_id["P4"].graph_rank == 1
    assert by_id["P4"].claim_id == "C4"
    assert by_id["P4"].claim_type == "measurement"

    # P3 appears only in dense (#2)
    assert by_id["P3"].source == "vector"
    assert by_id["P3"].dense_rank == 2

    # P2 (3 engines) outranks P1 (2 engines), which outranks P4 and P3 (1 engine each)
    order = [c.passage_id for c in fused]
    assert order == ["P2", "P1", "P4", "P3"]


def test_query_seed_topic_extraction_normalizes_aliases() -> None:
    slugs, _terms = extract_query_seed_topics(
        "Which authors disagree on whether Retrieval-Augmented Generation "
        "requires a reranker or RRF?"
    )
    assert "rag" in slugs
    assert "reranker" in slugs
    assert "reciprocal-rank-fusion" in slugs

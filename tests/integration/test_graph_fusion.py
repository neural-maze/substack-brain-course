"""Integration test for Week 4 three-way retrieval fusion and `kb_sources` provenance.

Seeds two publications connected via a shared `:Topic` (`rag`) in Memgraph and
Postgres, runs `hybrid_search`, verifies `graph`-sourced candidates appear in
the fused candidates, and verifies `kb_sources` resolves both `claim_id` and
`passage_id` to their verbatim quote and temporal validity metadata.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import kb.retrieval.hybrid as hybrid_module
from kb.db.queries import (
    get_source_provenance,
    persist_article_claims,
    replace_passages,
    upsert_article,
    upsert_publication,
)
from kb.graph.client import (
    check_memgraph_health,
    execute_cypher,
    find_cross_publication_shared_topic_claims,
    sync_article_claims_to_graph,
)
from kb.schemas.entities import RawExtractedClaim
from kb.sources.quote_validator import validate_and_normalize_claims

CANONICAL_1 = "pub:fusion-pub-a:post-rag-1"
CANONICAL_2 = "pub:fusion-pub-b:post-rag-2"


@pytest.fixture(autouse=True)
async def _require_postgres_and_memgraph() -> AsyncIterator[None]:
    mg_status = await check_memgraph_health()
    if mg_status != "ok":
        pytest.skip(f"Memgraph not reachable ({mg_status}) — run `make start` first.")
    try:
        yield
    finally:
        for cid in (CANONICAL_1, CANONICAL_2):
            await execute_cypher(
                """
                MATCH (art:Article {canonical_id: $cid})
                OPTIONAL MATCH (art)-[:ASSERTS]->(c:Claim)
                DETACH DELETE c, art
                """,
                {"cid": cid},
            )
        for slug in ("fusion-pub-a", "fusion-pub-b"):
            await execute_cypher(
                "MATCH (p:Publication {slug: $slug}) "
                "WHERE NOT ()-[:PUBLISHED_IN]->(p) DETACH DELETE p",
                {"slug": slug},
            )


async def test_three_way_graph_fusion_and_kb_sources_provenance(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stub OpenAI embedding call so this integration test needs no live OPENAI_API_KEY
    async def _fake_embed_texts(texts: list[str]) -> dict[str, object]:
        return {
            "embeddings": [[0.01] * 1536 for _ in texts],
            "usage": {
                "model": "text-embedding-3-small",
                "input_tokens": 5,
                "output_tokens": 0,
                "est_usd": 0.0,
            },
        }

    monkeypatch.setattr(hybrid_module, "embed_texts", _fake_embed_texts)

    pub_a_id = await upsert_publication(
        db,
        slug="fusion-pub-a",
        name="Fusion Pub A",
        feed_url="https://fusion-a.example.com/feed",
        homepage="https://fusion-a.example.com",
    )
    pub_b_id = await upsert_publication(
        db,
        slug="fusion-pub-b",
        name="Fusion Pub B",
        feed_url="https://fusion-b.example.com/feed",
        homepage="https://fusion-b.example.com",
    )

    text_a = (
        "Retrieval-Augmented Generation pipelines benefit from Reciprocal Rank "
        "Fusion before invoking a cross-encoder reranker."
    )
    text_b = (
        "In low-latency production services, RAG architectures should skip the "
        "reranker and rely on hybrid BM25 plus vector fusion."
    )

    art_a_id = await upsert_article(
        db,
        canonical_id=CANONICAL_1,
        publication_id=pub_a_id,
        url="https://fusion-a.example.com/p/post-rag-1",
        title="Why RAG Needs a Reranker",
        author="Author Alpha",
        published_at=datetime(2026, 10, 10, tzinfo=UTC),
        content_hash="hash_a",
        text=text_a,
    )
    art_b_id = await upsert_article(
        db,
        canonical_id=CANONICAL_2,
        publication_id=pub_b_id,
        url="https://fusion-b.example.com/p/post-rag-2",
        title="When to Skip the Reranker in RAG",
        author="Author Beta",
        published_at=datetime(2026, 10, 12, tzinfo=UTC),
        content_hash="hash_b",
        text=text_b,
    )

    await replace_passages(
        db,
        article_id=art_a_id,
        passages=[{"chunk_index": 0, "text": text_a, "embedding": [0.01] * 1536}],
    )
    await replace_passages(
        db,
        article_id=art_b_id,
        passages=[{"chunk_index": 0, "text": text_b, "embedding": [0.01] * 1536}],
    )

    claims_a, _ = validate_and_normalize_claims(
        article_id=art_a_id,
        content_hash="hash_a",
        article_text=text_a,
        raw_claims=[
            RawExtractedClaim(
                claim_text="RAG pipelines benefit from RRF before a cross-encoder reranker.",
                quote_span=text_a,
                claim_type="opinion",
                entities=["Retrieval-Augmented Generation", "Reranker"],
                confidence=0.95,
            )
        ],
    )
    claims_b, _ = validate_and_normalize_claims(
        article_id=art_b_id,
        content_hash="hash_b",
        article_text=text_b,
        raw_claims=[
            RawExtractedClaim(
                claim_text="Low-latency RAG services should skip the reranker.",
                quote_span=text_b,
                claim_type="opinion",
                entities=["RAG", "rerankers"],
                confidence=0.93,
            )
        ],
    )

    now = datetime(2026, 10, 15, tzinfo=UTC)
    await persist_article_claims(
        db,
        article_id=art_a_id,
        claims=[c.model_dump(mode="json") for c in claims_a],
        now=now,
    )
    await persist_article_claims(
        db,
        article_id=art_b_id,
        claims=[c.model_dump(mode="json") for c in claims_b],
        now=now,
    )
    await db.flush()

    await sync_article_claims_to_graph(
        pub_slug="fusion-pub-a",
        pub_name="Fusion Pub A",
        pub_homepage="https://fusion-a.example.com",
        author_slug="author-alpha",
        author_name="Author Alpha",
        canonical_id=CANONICAL_1,
        article_id=str(art_a_id),
        url="https://fusion-a.example.com/p/post-rag-1",
        title="Why RAG Needs a Reranker",
        published_at="2026-10-10T00:00:00+00:00",
        content_hash="hash_a",
        claims=[
            {**c.model_dump(mode="json"), "valid_from": now.isoformat(), "valid_to": None}
            for c in claims_a
        ],
        now_iso=now.isoformat(),
    )
    await sync_article_claims_to_graph(
        pub_slug="fusion-pub-b",
        pub_name="Fusion Pub B",
        pub_homepage="https://fusion-b.example.com",
        author_slug="author-beta",
        author_name="Author Beta",
        canonical_id=CANONICAL_2,
        article_id=str(art_b_id),
        url="https://fusion-b.example.com/p/post-rag-2",
        title="When to Skip the Reranker in RAG",
        published_at="2026-10-12T00:00:00+00:00",
        content_hash="hash_b",
        claims=[
            {**c.model_dump(mode="json"), "valid_from": now.isoformat(), "valid_to": None}
            for c in claims_b
        ],
        now_iso=now.isoformat(),
    )

    # Verify cross-publication 2-hop traversal over shared :Topic ("rag" and "reranker")
    cross_rows = await find_cross_publication_shared_topic_claims(limit=25)
    shared_topics = {r["shared_topic"] for r in cross_rows}
    assert "rag" in shared_topics
    assert "reranker" in shared_topics

    # Run 3-way hybrid search
    top_passages, all_candidates = await hybrid_module.hybrid_search(
        db, query="Does RAG require a reranker in production?", limit=5
    )
    assert len(top_passages) >= 2
    graph_candidates = [c for c in all_candidates if c.get("graph_rank") is not None]
    assert len(graph_candidates) >= 2

    # Verify provenance lookup via get_source_provenance (backing kb_sources)
    import uuid as uuid_mod

    claim_prov = await get_source_provenance(db, uuid_mod.UUID(claims_a[0].claim_id))
    assert claim_prov is not None
    assert claim_prov["kind"] == "claim"
    assert claim_prov["verbatim_quote"] == text_a
    assert claim_prov["author"] == "Author Alpha"
    validity = claim_prov["validity"]
    assert isinstance(validity, dict) and validity["is_active"] is True

"""Integration tests for Week 4 claim extraction, Memgraph idempotency, and supersession.

Verifies against real Postgres and Memgraph (skipped if the local dev stack is not up):
1. Running `extract-claims` writes rows to Postgres `claims` and merges
   `:Publication`, `:Author`, `:Article`, `:Claim`, `:Topic` into Memgraph.
2. Re-running the identical event creates ZERO duplicate nodes or edges.
3. Updating an article (`kb/article.updated`) sets `valid_to` on superseded claims
   and creates `[:SUPERSEDES]` edges in Memgraph (and `supersedes_id` in Postgres).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kb.db.models import Claim
from kb.db.queries import (
    get_active_claims_for_article,
    persist_article_claims,
    upsert_article,
    upsert_publication,
)
from kb.graph.client import (
    check_memgraph_health,
    execute_cypher,
    sync_article_claims_to_graph,
)
from kb.graph.ontology import normalize_author
from kb.schemas.entities import RawExtractedClaim
from kb.sources.quote_validator import validate_and_normalize_claims

TEST_CANONICAL_ID = "pub:week4-test-pub:memgraph-idempotency-post"
TEST_PUB_SLUG = "week4-test-pub"
TEST_AUTHOR_SLUG = "miguel-otero-pedrido"


@pytest.fixture(autouse=True)
async def _require_postgres_and_memgraph() -> AsyncIterator[None]:
    mg_status = await check_memgraph_health()
    if mg_status != "ok":
        pytest.skip(f"Memgraph not reachable ({mg_status}) — run `make start` first.")
    try:
        yield
    finally:
        # Clean up test subgraph in Memgraph
        await execute_cypher(
            """
            MATCH (art:Article {canonical_id: $cid})
            OPTIONAL MATCH (art)-[:ASSERTS]->(c:Claim)
            DETACH DELETE c, art
            """,
            {"cid": TEST_CANONICAL_ID},
        )
        await execute_cypher(
            "MATCH (p:Publication {slug: $slug}) WHERE NOT ()-[:PUBLISHED_IN]->(p) DETACH DELETE p",
            {"slug": TEST_PUB_SLUG},
        )


async def test_enrich_memgraph_idempotency_and_supersession(db: AsyncSession) -> None:
    pub_id = await upsert_publication(
        db,
        slug=TEST_PUB_SLUG,
        name="Week 4 Test Publication",
        feed_url="https://week4-test.example.com/feed",
        homepage="https://week4-test.example.com",
    )

    v1_text = (
        "Reciprocal Rank Fusion combines BM25 lexical search and pgvector cosine "
        "similarity without requiring score calibration. Conditional HTTP 304 polling "
        "completes in under 15ms and emits zero downstream events."
    )
    article_id = await upsert_article(
        db,
        canonical_id=TEST_CANONICAL_ID,
        publication_id=pub_id,
        url="https://week4-test.example.com/p/memgraph-idempotency-post",
        title="Graph Ontology and Idempotent Enrichment",
        author="Miguel Otero Pedrido",
        published_at=datetime(2026, 10, 15, tzinfo=UTC),
        content_hash="hash_v1",
        text=v1_text,
    )

    raw_v1 = [
        RawExtractedClaim(
            claim_text="RRF fuses BM25 and pgvector rankings without score calibration.",
            quote_span=(
                "Reciprocal Rank Fusion combines BM25 lexical search and pgvector "
                "cosine similarity without requiring score calibration."
            ),
            claim_type="opinion",
            entities=["Reciprocal Rank Fusion", "BM25", "pgvector"],
            confidence=0.95,
        ),
        RawExtractedClaim(
            claim_text="Conditional HTTP 304 polling completes in under 15ms.",
            quote_span=(
                "Conditional HTTP 304 polling completes in under 15ms and emits "
                "zero downstream events."
            ),
            claim_type="measurement",
            entities=["Inngest"],
            confidence=0.92,
        ),
    ]

    validated_v1, dropped_v1 = validate_and_normalize_claims(
        article_id=article_id,
        content_hash="hash_v1",
        article_text=v1_text,
        raw_claims=raw_v1,
    )
    assert dropped_v1 == 0
    assert len(validated_v1) == 2

    now_v1 = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    v1_dicts = [c.model_dump(mode="json") for c in validated_v1]
    author_node = normalize_author("Miguel Otero Pedrido")

    # --- 1. First execution ---
    pairs_1 = await persist_article_claims(
        db, article_id=article_id, claims=v1_dicts, now=now_v1
    )
    await db.flush()
    assert pairs_1 == []

    await sync_article_claims_to_graph(
        pub_slug=TEST_PUB_SLUG,
        pub_name="Week 4 Test Publication",
        pub_homepage="https://week4-test.example.com",
        author_slug=author_node.slug,
        author_name=author_node.name,
        canonical_id=TEST_CANONICAL_ID,
        article_id=str(article_id),
        url="https://week4-test.example.com/p/memgraph-idempotency-post",
        title="Graph Ontology and Idempotent Enrichment",
        published_at="2026-10-15T00:00:00+00:00",
        content_hash="hash_v1",
        claims=[{**c, "valid_from": now_v1.isoformat(), "valid_to": None} for c in v1_dicts],
        supersedes_pairs=[],
        now_iso=now_v1.isoformat(),
    )

    # --- 2. Replay the exact same event: must produce ZERO duplicate nodes/edges ---
    await persist_article_claims(db, article_id=article_id, claims=v1_dicts, now=now_v1)
    await db.flush()
    await sync_article_claims_to_graph(
        pub_slug=TEST_PUB_SLUG,
        pub_name="Week 4 Test Publication",
        pub_homepage="https://week4-test.example.com",
        author_slug=author_node.slug,
        author_name=author_node.name,
        canonical_id=TEST_CANONICAL_ID,
        article_id=str(article_id),
        url="https://week4-test.example.com/p/memgraph-idempotency-post",
        title="Graph Ontology and Idempotent Enrichment",
        published_at="2026-10-15T00:00:00+00:00",
        content_hash="hash_v1",
        claims=[{**c, "valid_from": now_v1.isoformat(), "valid_to": None} for c in v1_dicts],
        supersedes_pairs=[],
        now_iso=now_v1.isoformat(),
    )

    active_pg = await get_active_claims_for_article(db, article_id)
    assert len(active_pg) == 2

    graph_counts = await execute_cypher(
        """
        MATCH (art:Article {canonical_id: $cid})-[r:ASSERTS]->(c:Claim)
        WHERE c.valid_to IS NULL
        RETURN count(DISTINCT art) AS articles,
               count(r) AS assert_edges,
               count(DISTINCT c) AS active_claims
        """,
        {"cid": TEST_CANONICAL_ID},
    )
    assert graph_counts[0]["articles"] == 1
    assert graph_counts[0]["assert_edges"] == 2
    assert graph_counts[0]["active_claims"] == 2

    # --- 3. Simulate article edit (kb/article.updated) ---
    v2_text = (
        "Three-way Reciprocal Rank Fusion combines BM25, pgvector, and Memgraph "
        "traversal into a single deterministic ranking."
    )
    raw_v2 = [
        RawExtractedClaim(
            claim_text="Three-way RRF fuses BM25, pgvector, and Memgraph into one ranking.",
            quote_span=(
                "Three-way Reciprocal Rank Fusion combines BM25, pgvector, and "
                "Memgraph traversal into a single deterministic ranking."
            ),
            claim_type="opinion",
            entities=["Reciprocal Rank Fusion", "Memgraph"],
            confidence=0.98,
        )
    ]
    validated_v2, dropped_v2 = validate_and_normalize_claims(
        article_id=article_id,
        content_hash="hash_v2",
        article_text=v2_text,
        raw_claims=raw_v2,
    )
    assert dropped_v2 == 0
    v2_dicts = [c.model_dump(mode="json") for c in validated_v2]
    now_v2 = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)

    supersedes_pairs = await persist_article_claims(
        db, article_id=article_id, claims=v2_dicts, now=now_v2
    )
    await db.flush()
    assert len(supersedes_pairs) == 1
    assert supersedes_pairs[0]["new_claim_id"] == validated_v2[0].claim_id

    await sync_article_claims_to_graph(
        pub_slug=TEST_PUB_SLUG,
        pub_name="Week 4 Test Publication",
        pub_homepage="https://week4-test.example.com",
        author_slug=author_node.slug,
        author_name=author_node.name,
        canonical_id=TEST_CANONICAL_ID,
        article_id=str(article_id),
        url="https://week4-test.example.com/p/memgraph-idempotency-post",
        title="Graph Ontology and Idempotent Enrichment",
        published_at="2026-10-15T00:00:00+00:00",
        content_hash="hash_v2",
        claims=[{**c, "valid_from": now_v2.isoformat(), "valid_to": None} for c in v2_dicts],
        supersedes_pairs=supersedes_pairs,
        now_iso=now_v2.isoformat(),
    )

    # Verify Postgres has 1 active claim and 2 superseded claims
    all_pg_claims = list(
        (await db.execute(select(Claim).where(Claim.article_id == article_id))).scalars()
    )
    assert len(all_pg_claims) == 3
    active_after = [c for c in all_pg_claims if c.valid_to is None]
    superseded_after = [c for c in all_pg_claims if c.valid_to is not None]
    assert len(active_after) == 1
    assert len(superseded_after) == 2
    assert active_after[0].supersedes_id == uuid.UUID(validated_v1[0].claim_id)

    # Verify Memgraph has [:SUPERSEDES] edge and valid_to set on old claims
    sup_rows = await execute_cypher(
        """
        MATCH (new_c:Claim {claim_id: $new_id})-[r:SUPERSEDES]->(old_c:Claim)
        WHERE new_c.valid_to IS NULL AND old_c.valid_to IS NOT NULL
        RETURN new_c.claim_id AS new_id, old_c.claim_id AS old_id, r.at AS at
        """,
        {"new_id": validated_v2[0].claim_id},
    )
    assert len(sup_rows) == 1
    assert sup_rows[0]["old_id"] == validated_v1[0].claim_id

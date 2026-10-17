"""Memgraph async Bolt client, schema initialization, and graph operations.

Uses `neo4j.AsyncGraphDatabase` when installed, with automatic fallback to
`kb.graph._bolt.FallbackBoltDriver` so parameterized Cypher works against
Memgraph even in environments where `neo4j` has not yet been synced into `.venv`.
"""

from __future__ import annotations

import logging
from typing import Any

from kb.config import settings
from kb.graph._bolt import FallbackBoltDriver
from kb.graph.cypher import (
    COUNT_ACTIVE_CLAIMS_AND_TOPICS_CYPHER,
    CROSS_PUBLICATION_DISAGREEMENTS_CYPHER,
    GET_CLAIM_PROVENANCE_CYPHER,
    MERGE_ARTICLE_SKELETON_CYPHER,
    MERGE_CLAIMS_BATCH_CYPHER,
    MERGE_SUPERSEDES_EDGES_CYPHER,
    SEARCH_CLAIMS_BY_TOPICS_CYPHER,
    SUPERSEDE_ARTICLE_CLAIMS_CYPHER,
)

try:
    from neo4j import AsyncGraphDatabase
except ImportError:  # pragma: no cover
    AsyncGraphDatabase = None

logger = logging.getLogger(__name__)

_driver: Any = None
_schema_initialized: bool = False

SCHEMA_DDL_STATEMENTS: tuple[str, ...] = (
    # Label-property indexes for O(1) MERGE and lookup performance
    "CREATE INDEX ON :Publication(slug);",
    "CREATE INDEX ON :Author(slug);",
    "CREATE INDEX ON :Article(canonical_id);",
    "CREATE INDEX ON :Article(article_id);",
    "CREATE INDEX ON :Claim(claim_id);",
    "CREATE INDEX ON :Claim(article_id);",
    "CREATE INDEX ON :Topic(slug);",
    # Uniqueness constraints (Memgraph Cypher syntax)
    "CREATE CONSTRAINT ON (p:Publication) ASSERT p.slug IS UNIQUE;",
    "CREATE CONSTRAINT ON (a:Author) ASSERT a.slug IS UNIQUE;",
    "CREATE CONSTRAINT ON (art:Article) ASSERT art.canonical_id IS UNIQUE;",
    "CREATE CONSTRAINT ON (c:Claim) ASSERT c.claim_id IS UNIQUE;",
    "CREATE CONSTRAINT ON (t:Topic) ASSERT t.slug IS UNIQUE;",
)


def get_graph_driver() -> Any:
    """Return the singleton async Bolt driver for Memgraph."""
    global _driver
    if _driver is None:
        auth = (settings.memgraph_user, settings.memgraph_password)
        if AsyncGraphDatabase is not None:
            _driver = AsyncGraphDatabase.driver(settings.memgraph_uri, auth=auth)
        else:
            _driver = FallbackBoltDriver(settings.memgraph_uri, auth=auth)
    return _driver


def set_graph_driver(driver: Any) -> None:
    """Override the singleton graph driver (used by tests)."""
    global _driver, _schema_initialized
    _driver = driver
    _schema_initialized = False


async def close_graph_driver() -> None:
    """Close and reset the singleton graph driver."""
    global _driver, _schema_initialized
    if _driver is not None:
        await _driver.close()
        _driver = None
        _schema_initialized = False


async def execute_cypher(
    query: str, parameters: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Execute a parameterized Cypher query and return rows as a list of dicts."""
    driver = get_graph_driver()
    async with driver.session() as session:
        result = await session.run(query, parameters or {})
        return list(await result.data())


async def ensure_graph_schema() -> None:
    """Idempotently ensure Memgraph indexes and uniqueness constraints exist."""
    global _schema_initialized
    if _schema_initialized:
        return
    driver = get_graph_driver()
    async with driver.session() as session:
        for stmt in SCHEMA_DDL_STATEMENTS:
            try:
                res = await session.run(stmt)
                await res.data()
            except Exception as exc:
                # Memgraph may report an error if a constraint/index already exists
                logger.debug("Memgraph schema statement skipped (%s): %s", stmt, exc)
    _schema_initialized = True


async def check_memgraph_health() -> str:
    """Check whether Memgraph is reachable over Bolt and ensure schema is initialized."""
    try:
        rows = await execute_cypher("RETURN 1 AS ok")
        if rows and rows[0].get("ok") == 1:
            await ensure_graph_schema()
            return "ok"
        return "error: unexpected response from Memgraph"
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc}"


async def count_graph_entities() -> tuple[int, int]:
    """Return `(active_claims, active_topics)` from Memgraph."""
    rows = await execute_cypher(COUNT_ACTIVE_CLAIMS_AND_TOPICS_CYPHER)
    if not rows:
        return 0, 0
    row = rows[0]
    return int(row.get("active_claims", 0) or 0), int(row.get("active_topics", 0) or 0)


async def sync_article_claims_to_graph(
    *,
    pub_slug: str,
    pub_name: str,
    pub_homepage: str,
    author_slug: str,
    author_name: str,
    canonical_id: str,
    article_id: str,
    url: str,
    title: str,
    published_at: str,
    content_hash: str,
    claims: list[dict[str, Any]],
    supersedes_pairs: list[dict[str, str]] | None = None,
    now_iso: str,
) -> dict[str, int]:
    """Idempotently synchronize an article's skeleton, active claims, topics,
    and supersession edges into Memgraph.
    """
    await ensure_graph_schema()

    await execute_cypher(
        MERGE_ARTICLE_SKELETON_CYPHER,
        {
            "pub_slug": pub_slug,
            "pub_name": pub_name,
            "pub_homepage": pub_homepage,
            "author_slug": author_slug,
            "author_name": author_name,
            "canonical_id": canonical_id,
            "article_id": article_id,
            "url": url,
            "title": title,
            "published_at": published_at,
            "content_hash": content_hash,
        },
    )

    active_claim_ids = [str(c["claim_id"]) for c in claims]
    superseded_rows = await execute_cypher(
        SUPERSEDE_ARTICLE_CLAIMS_CYPHER,
        {
            "canonical_id": canonical_id,
            "active_claim_ids": active_claim_ids,
            "now": now_iso,
        },
    )
    superseded_count = (
        int(superseded_rows[0].get("superseded_count", 0)) if superseded_rows else 0
    )

    synced_claims = 0
    if claims:
        batch_rows = await execute_cypher(
            MERGE_CLAIMS_BATCH_CYPHER,
            {
                "canonical_id": canonical_id,
                "claims": claims,
            },
        )
        synced_claims = (
            int(batch_rows[0].get("synced_claims", len(claims)))
            if batch_rows
            else len(claims)
        )

    supersedes_edges = 0
    if supersedes_pairs:
        edge_rows = await execute_cypher(
            MERGE_SUPERSEDES_EDGES_CYPHER,
            {"supersedes_pairs": supersedes_pairs},
        )
        supersedes_edges = (
            int(edge_rows[0].get("supersedes_edges", len(supersedes_pairs)))
            if edge_rows
            else len(supersedes_pairs)
        )

    return {
        "synced_claims": synced_claims,
        "superseded_claims": superseded_count,
        "supersedes_edges": supersedes_edges,
    }


async def search_graph_by_topics(
    *,
    topic_slugs: list[str],
    search_terms: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    """Run the 2-hop topic-to-claim Cypher traversal over active claims."""
    if not topic_slugs and not search_terms:
        return []
    return await execute_cypher(
        SEARCH_CLAIMS_BY_TOPICS_CYPHER,
        {
            "topic_slugs": topic_slugs,
            "search_terms": search_terms,
            "limit": limit,
        },
    )


async def get_claim_provenance_from_graph(
    claim_id: str, *, include_superseded: bool = True
) -> dict[str, Any] | None:
    """Fetch claim provenance and connected topic nodes from Memgraph."""
    rows = await execute_cypher(
        GET_CLAIM_PROVENANCE_CYPHER,
        {"claim_id": claim_id, "include_superseded": include_superseded},
    )
    return rows[0] if rows else None


async def find_cross_publication_shared_topic_claims(
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Traverse Author 1 -> Article -> Claim -> Topic <- Claim <- Article <- Author 2."""
    return await execute_cypher(
        CROSS_PUBLICATION_DISAGREEMENTS_CYPHER,
        {"limit": limit},
    )

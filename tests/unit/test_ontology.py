"""Unit tests for Phase 0 ontology and deterministic topic/author normalization."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kb.graph.ontology import (
    EDGE_TYPES,
    NODE_LABELS,
    normalize_author,
    normalize_topic,
    normalize_topics,
)
from kb.schemas.entities import RawExtractedClaim
from kb.schemas.ids import claim_id


def test_locked_ontology_has_five_node_labels_and_five_edge_types() -> None:
    expected_nodes = {"Publication", "Author", "Article", "Claim", "Topic"}
    expected_edges = {"PUBLISHED_IN", "WRITTEN_BY", "ASSERTS", "ABOUT", "SUPERSEDES"}
    assert set(NODE_LABELS) == expected_nodes
    assert set(EDGE_TYPES) == expected_edges


def test_rag_synonyms_collapse_to_single_canonical_topic() -> None:
    variants = [
        "RAG",
        "rag",
        "Retrieval-Augmented Generation",
        "retrieval augmented generation",
        "  Retrieval-Augmented Generation! ",
    ]
    normalized = [normalize_topic(v) for v in variants]
    assert all(t is not None and t.slug == "rag" and t.name == "RAG" for t in normalized)


def test_core_domain_aliases_resolve_deterministically() -> None:
    rrf = normalize_topic("Reciprocal Rank Fusion")
    rrf_short = normalize_topic("RRF")
    assert rrf is not None and rrf_short is not None
    assert rrf.slug == rrf_short.slug == "reciprocal-rank-fusion"

    llm_1 = normalize_topic("Large Language Models")
    llm_2 = normalize_topic("LLMs")
    assert llm_1 is not None and llm_2 is not None
    assert llm_1.slug == llm_2.slug == "llm"

    mcp_1 = normalize_topic("Model Context Protocol")
    mcp_2 = normalize_topic("mcp")
    assert mcp_1 is not None and mcp_2 is not None
    assert mcp_1.slug == mcp_2.slug == "mcp"


def test_depluralization_collapses_plural_technical_nouns() -> None:
    singular = normalize_topic("embedding model")
    plural = normalize_topic("Embedding Models")
    assert singular is not None and plural is not None
    assert singular.slug == plural.slug == "embedding-model"


def test_stop_topics_are_rejected() -> None:
    stop_words = (
        "ai",
        "AI",
        "system",
        "Systems",
        "code",
        "article",
        "author",
        "data",
        "model",
        "models",
    )
    for stop_word in stop_words:
        assert normalize_topic(stop_word) is None


def test_normalize_topics_deduplicates_and_filters_stop_topics() -> None:
    raw = ["RAG", "Retrieval-Augmented Generation", "AI", "pgvector", "models", "RRF"]
    topics = normalize_topics(raw)
    assert [t.slug for t in topics] == ["rag", "pgvector", "reciprocal-rank-fusion"]


def test_author_normalization_and_fallback() -> None:
    author = normalize_author("Miguel Otero Pedrido")
    assert author.slug == "miguel-otero-pedrido"
    assert author.name == "Miguel Otero Pedrido"

    unknown = normalize_author(None)
    assert unknown.slug == "unknown-author"
    assert unknown.name == "Unknown Author"


def test_claim_type_enum_bounds_enforced() -> None:
    for valid_type in ("opinion", "measurement", "prediction"):
        claim = RawExtractedClaim(
            claim_text="Conditional HTTP 304 polling finishes in under 15ms.",
            quote_span=(
                "When nothing has changed, origins return 304 Not Modified "
                "and the run finishes in under 15ms."
            ),
            claim_type=valid_type,
            entities=["Inngest"],
        )
        assert claim.claim_type == valid_type

    with pytest.raises(ValidationError):
        RawExtractedClaim.model_validate(
            {
                "claim_text": "Invalid claim type example.",
                "quote_span": "Some verbatim quote from an article.",
                "claim_type": "speculation",
                "entities": ["RAG"],
            }
        )


def test_deterministic_claim_id_is_idempotent_across_whitespace_variations() -> None:
    id1 = claim_id(
        "00000000-0000-0000-0000-000000000001",
        "hash123",
        "When nothing has changed,   origins return 304.",
        "Origins return 304 when unchanged.",
    )
    id2 = claim_id(
        "00000000-0000-0000-0000-000000000001",
        "hash123",
        "When nothing has changed, origins return 304.",
        "Origins return 304   when unchanged.",
    )
    assert id1 == id2

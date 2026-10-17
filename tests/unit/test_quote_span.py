"""Unit tests for the deterministic quote-span guardrail (`kb/sources/quote_validator.py`)."""

from __future__ import annotations

import uuid

from kb.schemas.entities import RawExtractedClaim
from kb.sources.quote_validator import is_verbatim_quote, validate_and_normalize_claims

SAMPLE_ARTICLE = (
    "In production AI systems, Reciprocal Rank Fusion combines BM25 lexical search "
    "and pgvector cosine similarity without requiring score calibration.\n\n"
    "When nothing has changed, origins return 304 Not Modified with 0 body bytes, "
    "and the polling run finishes in under 15ms. However, flat passage retrieval "
    "cannot answer relational questions across multiple newsletters."
)


def test_exact_verbatim_substring_passes() -> None:
    quote = (
        "Reciprocal Rank Fusion combines BM25 lexical search and pgvector cosine "
        "similarity without requiring score calibration."
    )
    assert is_verbatim_quote(SAMPLE_ARTICLE, quote) is True


def test_whitespace_normalized_multiline_substring_passes() -> None:
    quote = (
        "When nothing has changed,   origins return 304 Not Modified\n"
        "with 0 body bytes, and the polling run finishes in under 15ms."
    )
    assert is_verbatim_quote(SAMPLE_ARTICLE, quote) is True


def test_paraphrased_or_fabricated_quote_is_rejected() -> None:
    paraphrased = (
        "Reciprocal Rank Fusion merges BM25 and pgvector scores without needing calibration."
    )
    fabricated = "Agentic RAG always requires a cross-encoder reranker in production."
    ellipsis_truncated = (
        "Reciprocal Rank Fusion combines BM25 ... without requiring score calibration."
    )
    assert is_verbatim_quote(SAMPLE_ARTICLE, paraphrased) is False
    assert is_verbatim_quote(SAMPLE_ARTICLE, fabricated) is False
    assert is_verbatim_quote(SAMPLE_ARTICLE, ellipsis_truncated) is False


def test_too_short_or_empty_quote_span_is_rejected() -> None:
    assert is_verbatim_quote(SAMPLE_ARTICLE, "") is False
    assert is_verbatim_quote(SAMPLE_ARTICLE, "in under 15ms") is False


def test_validate_and_normalize_claims_keeps_valid_and_counts_dropped() -> None:
    aid = uuid.UUID("00000000-0000-0000-0000-000000000099")
    raw_claims = [
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
            claim_text="Conditional HTTP 304 polling finishes in under 15ms.",
            quote_span=(
                "When nothing has changed, origins return 304 Not Modified with 0 body "
                "bytes, and the polling run finishes in under 15ms."
            ),
            claim_type="measurement",
            entities=["HTTP 304", "AI"],
            confidence=0.92,
        ),
        # Paraphrased / non-verbatim quote -> must be dropped
        RawExtractedClaim(
            claim_text="Hallucinated paraphrase that alters words from the text.",
            quote_span="Origins return 304 and finish in less than 15 milliseconds.",
            claim_type="measurement",
            entities=["Inngest"],
            confidence=0.80,
        ),
        # Only stop-topics and no fallback -> must be dropped
        RawExtractedClaim(
            claim_text="Flat passage retrieval cannot answer relational questions.",
            quote_span=(
                "However, flat passage retrieval cannot answer relational "
                "questions across multiple newsletters."
            ),
            claim_type="opinion",
            entities=["AI", "system", "models"],
            confidence=0.85,
        ),
    ]

    validated, dropped = validate_and_normalize_claims(
        article_id=aid,
        content_hash="sha256_abc",
        article_text=SAMPLE_ARTICLE,
        raw_claims=raw_claims,
    )

    assert len(validated) == 2
    assert dropped == 2
    assert validated[0].claim_type == "opinion"
    assert [t.slug for t in validated[0].topics] == [
        "reciprocal-rank-fusion",
        "bm25",
        "pgvector",
    ]
    assert validated[1].claim_type == "measurement"
    assert [t.slug for t in validated[1].topics] == ["http-304"]

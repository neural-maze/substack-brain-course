"""The First Free Guardrail: deterministic verbatim quote-span verification.

Before any LLM-extracted claim touches PostgreSQL or Memgraph, this module
verifies that `quote_span` is a real verbatim substring of `article_text`
(modulo consecutive whitespace normalization) and canonicalizes its topics.
Any fabricated, paraphrased, or truncated quote span is dropped immediately.
"""

from __future__ import annotations

import re
import unicodedata
import uuid

from kb.graph.ontology import normalize_topic, normalize_topics
from kb.schemas.entities import RawExtractedClaim, ValidatedClaim
from kb.schemas.ids import claim_id

MIN_QUOTE_SPAN_CHARS = 15


def normalize_whitespace(text: str) -> str:
    """Collapse consecutive whitespace into a single space while preserving
    exact words, casing, and punctuation.
    """
    normalized = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", normalized).strip()


def is_verbatim_quote(
    article_text: str,
    quote_span: str,
    *,
    min_chars: int = MIN_QUOTE_SPAN_CHARS,
) -> bool:
    """Return True iff `quote_span` is at least `min_chars` long and appears
    verbatim inside `article_text` after normalizing consecutive whitespace.
    """
    if not article_text or not quote_span:
        return False
    norm_quote = normalize_whitespace(quote_span)
    if len(norm_quote) < min_chars:
        return False
    norm_article = normalize_whitespace(article_text)
    return norm_quote in norm_article


def validate_and_normalize_claims(
    article_id: uuid.UUID | str,
    content_hash: str,
    article_text: str,
    raw_claims: list[RawExtractedClaim],
    *,
    fallback_topic: str | None = None,
) -> tuple[list[ValidatedClaim], int]:
    """Validate quote spans and canonicalize topics for a batch of raw LLM claims.

    Returns `(validated_claims, dropped_spans)`.
    """
    validated: list[ValidatedClaim] = []
    seen_claim_ids: set[str] = set()
    dropped_spans = 0

    for raw in raw_claims:
        if not is_verbatim_quote(article_text, raw.quote_span):
            dropped_spans += 1
            continue

        topics = normalize_topics(raw.entities)
        if not topics and fallback_topic:
            fallback = normalize_topic(fallback_topic)
            if fallback is not None:
                topics = [fallback]

        if not topics:
            dropped_spans += 1
            continue

        norm_quote = normalize_whitespace(raw.quote_span)
        norm_claim_text = normalize_whitespace(raw.claim_text)
        cid = str(claim_id(article_id, content_hash, norm_quote, norm_claim_text))

        if cid in seen_claim_ids:
            continue
        seen_claim_ids.add(cid)

        validated.append(
            ValidatedClaim(
                claim_id=cid,
                article_id=str(article_id),
                text=norm_claim_text,
                quote_span=norm_quote,
                claim_type=raw.claim_type,
                confidence=raw.confidence,
                topics=topics,
            )
        )

    return validated, dropped_spans

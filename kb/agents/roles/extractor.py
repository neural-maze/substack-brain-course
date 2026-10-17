"""Claim extractor agent role configured with the locked Week 4 ontology schema."""

from __future__ import annotations

import re

from kb.agents.core import Agent
from kb.agents.prompts import CLAIM_EXTRACTOR_SYSTEM_PROMPT
from kb.config import settings
from kb.graph.ontology import TOPIC_ALIASES, normalize_topics
from kb.schemas.entities import ClaimExtractionBatch, ClaimType, RawExtractedClaim

claim_extractor_agent: Agent[ClaimExtractionBatch] = Agent(
    name="claim-extractor",
    role="Structured Claim & Quote-Span Extractor",
    system_prompt=CLAIM_EXTRACTOR_SYSTEM_PROMPT,
    model=settings.extraction_model,
    output_schema=ClaimExtractionBatch,
    max_tokens=settings.extraction_max_tokens,
)


def format_extractor_prompt(
    *,
    title: str,
    author: str | None,
    publication_name: str,
    article_text: str,
) -> str:
    """Format the article metadata and body for `claim_extractor_agent`."""
    return (
        f"Publication: {publication_name}\n"
        f"Article Title: {title}\n"
        f"Author: {author or 'Unknown Author'}\n\n"
        f"Article Text:\n{article_text}"
    )


def heuristic_extract_claims(
    article_text: str,
    *,
    max_claims: int = 5,
) -> ClaimExtractionBatch:
    """Deterministic fallback extractor used only when `ANTHROPIC_API_KEY` is not set.

    Splits the article into sentences, finds sentences mentioning known domain
    entities, and uses the exact sentence from `article_text` as `quote_span`
    so the downstream quote-span guardrail still validates real substrings.
    """
    sentences = [
        s.strip()
        for s in re.split(r"(?<=[.!?])\s+", article_text)
        if len(s.strip()) >= 25
    ]
    extracted: list[RawExtractedClaim] = []

    for sentence in sentences:
        lower = sentence.lower()
        matched_entities: list[str] = []
        for alias_key, (_slug, canonical_name) in TOPIC_ALIASES.items():
            if re.search(rf"\b{re.escape(alias_key)}\b", lower):
                matched_entities.append(canonical_name)

        topics = normalize_topics(matched_entities)
        if not topics:
            continue

        claim_type: ClaimType = "opinion"
        if re.search(r"\b\d+(?:\.\d+)?\s*(?:ms|%|x|seconds|bytes|tokens|usd)\b", lower):
            claim_type = "measurement"
        elif re.search(r"\b(?:will|future|predict|next decade|eventually)\b", lower):
            claim_type = "prediction"

        extracted.append(
            RawExtractedClaim(
                claim_text=sentence,
                quote_span=sentence,
                claim_type=claim_type,
                entities=[t.name for t in topics[:5]],
                confidence=0.85,
            )
        )
        if len(extracted) >= max_claims:
            break

    return ClaimExtractionBatch(claims=extracted)


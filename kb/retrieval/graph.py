"""Graph-based claim retrieval over Memgraph (`kb/retrieval/graph.py`).

Extracts normalized topic seeds from a user query, traverses active `:Claim`
nodes connected to matching `:Topic` hubs in Memgraph (`WHERE c.valid_to IS NULL`),
and resolves their parent Postgres passages so 3-way Reciprocal Rank Fusion
can fuse BM25, pgvector, and graph candidates on a single ordinal scale.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from kb.config import settings
from kb.db.queries import find_matching_passage_for_quote
from kb.graph.client import search_graph_by_topics
from kb.graph.ontology import STOP_TOPICS, TOPIC_ALIASES, normalize_topic

logger = logging.getLogger(__name__)

_QUERY_STOPWORDS: frozenset[str] = STOP_TOPICS | frozenset(
    {
        "what",
        "which",
        "who",
        "how",
        "why",
        "when",
        "where",
        "does",
        "do",
        "did",
        "is",
        "are",
        "was",
        "were",
        "should",
        "can",
        "could",
        "would",
        "will",
        "have",
        "has",
        "had",
        "about",
        "across",
        "between",
        "from",
        "with",
        "into",
        "over",
        "under",
        "the",
        "and",
        "for",
        "not",
        "that",
        "this",
        "these",
        "those",
        "their",
        "there",
        "whether",
        "disagree",
        "agree",
        "authors",
        "publications",
        "newsletters",
        "engineers",
        "design",
        "production",
    }
)


def extract_query_seed_topics(query: str) -> tuple[list[str], list[str]]:
    """Deterministically extract `(topic_slugs, search_terms)` from a natural-language query."""
    lowered = query.lower()
    slugs: list[str] = []
    seen_slugs: set[str] = set()

    def _add_slug(s: str) -> None:
        if s and s not in seen_slugs and s not in _QUERY_STOPWORDS:
            seen_slugs.add(s)
            slugs.append(s)

    # 1. Check multi-word and acronym aliases from TOPIC_ALIASES
    for alias_key, (canonical_slug, _name) in TOPIC_ALIASES.items():
        if re.search(rf"\b{re.escape(alias_key)}\b", lowered):
            _add_slug(canonical_slug)

    # 2. Extract alphabetic/alphanumeric tokens and check unigrams, bigrams, trigrams
    tokens = [
        t
        for t in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", lowered)
        if t not in _QUERY_STOPWORDS and len(t) >= 2
    ]

    for n in (3, 2, 1):
        for i in range(len(tokens) - n + 1):
            phrase = " ".join(tokens[i : i + n])
            topic = normalize_topic(phrase)
            if topic is not None and topic.slug not in _QUERY_STOPWORDS:
                _add_slug(topic.slug)

    search_terms = [t for t in tokens if len(t) >= 3][:8]
    return slugs, search_terms


async def search_graph_claims(
    session: AsyncSession,
    query: str,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Traverse Memgraph for active claims matching query topics and map them
    to passage candidates for 3-way RRF fusion.
    """
    max_results = limit or settings.graph_retrieval_limit
    topic_slugs, search_terms = extract_query_seed_topics(query)
    if not topic_slugs and not search_terms:
        return []

    try:
        claim_rows = await search_graph_by_topics(
            topic_slugs=topic_slugs,
            search_terms=search_terms,
            limit=max_results,
        )
    except Exception as exc:
        logger.debug("Graph traversal unavailable for query %r: %s", query, exc)
        return []

    results: list[dict[str, Any]] = []
    for row in claim_rows:
        raw_article_id = str(row.get("article_id", ""))
        quote_span = str(row.get("quote_span", ""))
        claim_id_str = str(row.get("claim_id", ""))
        claim_text = str(row.get("claim_text", ""))
        claim_type = str(row.get("claim_type", "opinion"))

        passage_match: dict[str, object] | None = None
        try:
            aid = uuid.UUID(raw_article_id)
            passage_match = await find_matching_passage_for_quote(
                session, article_id=aid, quote_span=quote_span
            )
        except (ValueError, Exception):
            passage_match = None

        if passage_match is not None:
            results.append(
                {
                    "passage_id": str(passage_match["passage_id"]),
                    "text": str(passage_match["text"]),
                    "title": str(passage_match["title"]),
                    "author": passage_match.get("author") or row.get("author"),
                    "url": str(passage_match["url"]),
                    "published_at": str(passage_match["published_at"]),
                    "claim_id": claim_id_str,
                    "claim_text": claim_text,
                    "claim_type": claim_type,
                    "quote_span": quote_span,
                    "matched_topics": int(row.get("matched_topics", 1) or 1),
                    "rank": float(row.get("confidence", 0.9) or 0.9),
                }
            )
        else:
            # Fallback if passage rows aren't present: surface the verified claim directly
            results.append(
                {
                    "passage_id": claim_id_str,
                    "text": f"{claim_text} — \"{quote_span}\"",
                    "title": str(row.get("title", "Unknown")),
                    "author": row.get("author"),
                    "url": str(row.get("url", "")),
                    "published_at": str(row.get("published_at", "")),
                    "claim_id": claim_id_str,
                    "claim_text": claim_text,
                    "claim_type": claim_type,
                    "quote_span": quote_span,
                    "matched_topics": int(row.get("matched_topics", 1) or 1),
                    "rank": float(row.get("confidence", 0.9) or 0.9),
                }
            )

    return results


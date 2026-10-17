"""Phase 0: Locked Graph Ontology & Entity/Topic Canonicalization.

The ontology is strictly closed:
- 5 Node Labels: Publication, Author, Article, Claim, Topic
- 5 Edge Types: PUBLISHED_IN, WRITTEN_BY, ASSERTS, ABOUT, SUPERSEDES

All topic and author strings are deterministically canonicalized here before
any node is merged into Memgraph or persisted to Postgres.
"""

from __future__ import annotations

import re
import unicodedata

from pydantic import BaseModel, Field

from kb.schemas.entities import ClaimType, ExtractedTopic

NODE_LABELS: frozenset[str] = frozenset(
    {"Publication", "Author", "Article", "Claim", "Topic"}
)

EDGE_TYPES: frozenset[str] = frozenset(
    {"PUBLISHED_IN", "WRITTEN_BY", "ASSERTS", "ABOUT", "SUPERSEDES"}
)


class PublicationNode(BaseModel):
    slug: str
    name: str
    homepage: str


class AuthorNode(BaseModel):
    slug: str
    name: str


class ArticleNode(BaseModel):
    canonical_id: str
    article_id: str
    url: str
    title: str
    published_at: str
    content_hash: str


class ClaimNode(BaseModel):
    claim_id: str
    article_id: str
    text: str
    quote_span: str
    claim_type: ClaimType
    confidence: float = Field(ge=0.0, le=1.0)
    valid_from: str
    valid_to: str | None = None


class TopicNode(BaseModel):
    slug: str
    name: str


# Controlled vocabulary alias map for core AI engineering entities across publications.
# Keys may be raw lowercase strings or hyphen-normalized slugs.
TOPIC_ALIASES: dict[str, tuple[str, str]] = {
    # RAG
    "rag": ("rag", "RAG"),
    "retrieval augmented generation": ("rag", "RAG"),
    "retrieval-augmented generation": ("rag", "RAG"),
    "retrieval-augmented-generation": ("rag", "RAG"),
    # Agentic RAG
    "agentic rag": ("agentic-rag", "Agentic RAG"),
    "agentic-rag": ("agentic-rag", "Agentic RAG"),
    # RRF
    "rrf": ("reciprocal-rank-fusion", "Reciprocal Rank Fusion"),
    "reciprocal rank fusion": ("reciprocal-rank-fusion", "Reciprocal Rank Fusion"),
    "reciprocal-rank-fusion": ("reciprocal-rank-fusion", "Reciprocal Rank Fusion"),
    # LLM
    "llm": ("llm", "LLM"),
    "llms": ("llm", "LLM"),
    "large language model": ("llm", "LLM"),
    "large language models": ("llm", "LLM"),
    "large-language-model": ("llm", "LLM"),
    "large-language-models": ("llm", "LLM"),
    # SLM
    "slm": ("small-language-model", "Small Language Model"),
    "slms": ("small-language-model", "Small Language Model"),
    "small language model": ("small-language-model", "Small Language Model"),
    "small language models": ("small-language-model", "Small Language Model"),
    "small-language-model": ("small-language-model", "Small Language Model"),
    "small-language-models": ("small-language-model", "Small Language Model"),
    # MCP
    "mcp": ("mcp", "Model Context Protocol (MCP)"),
    "model context protocol": ("mcp", "Model Context Protocol (MCP)"),
    "model-context-protocol": ("mcp", "Model Context Protocol (MCP)"),
    # Storage & Orchestration
    "pgvector": ("pgvector", "pgvector"),
    "pg-vector": ("pgvector", "pgvector"),
    "inngest": ("inngest", "Inngest"),
    "memgraph": ("memgraph", "Memgraph"),
    "bm25": ("bm25", "BM25"),
    "bm-25": ("bm25", "BM25"),
    # Retrieval & Reranking
    "hybrid search": ("hybrid-retrieval", "Hybrid Retrieval"),
    "hybrid-search": ("hybrid-retrieval", "Hybrid Retrieval"),
    "hybrid retrieval": ("hybrid-retrieval", "Hybrid Retrieval"),
    "hybrid-retrieval": ("hybrid-retrieval", "Hybrid Retrieval"),
    "reranker": ("reranker", "Reranker"),
    "rerankers": ("reranker", "Reranker"),
    "reranking": ("reranker", "Reranker"),
    "re-ranker": ("reranker", "Reranker"),
    "re-ranking": ("reranker", "Reranker"),
    # Compute & Orchestration concepts
    "test time compute": ("test-time-compute", "Test-Time Compute"),
    "test-time compute": ("test-time-compute", "Test-Time Compute"),
    "test-time-compute": ("test-time-compute", "Test-Time Compute"),
    "inference-time-compute": ("test-time-compute", "Test-Time Compute"),
    "durable execution": ("durable-execution", "Durable Execution"),
    "durable-execution": ("durable-execution", "Durable Execution"),
}

# Overly generic terms rejected so Topic nodes never become high-degree supernode hairballs.
STOP_TOPICS: frozenset[str] = frozenset(
    {
        "ai",
        "system",
        "systems",
        "code",
        "article",
        "articles",
        "author",
        "authors",
        "data",
        "model",
        "models",
        "software",
        "tool",
        "tools",
        "technology",
        "thing",
        "things",
        "method",
        "methods",
        "approach",
        "approaches",
        "use",
        "way",
    }
)

_SINGULAR_EXCEPTIONS: frozenset[str] = frozenset(
    {
        "postgres",
        "redis",
        "serverless",
        "analytics",
        "metrics",
        "mathematics",
        "physics",
        "statistics",
        "ethics",
        "semantics",
        "heuristics",
        "logistics",
        "dynamics",
        "corpus",
        "status",
        "bias",
        "pass",
        "class",
    }
)


def _depluralize_token(token: str) -> str:
    """De-pluralize simple trailing 's' on technical nouns where safe."""
    if len(token) <= 3 or token in _SINGULAR_EXCEPTIONS:
        return token
    if token.endswith(("ss", "us", "is", "as", "os", "ics")):
        return token
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("s") and not token.endswith("es"):
        return token[:-1]
    if (
        token.endswith("es")
        and len(token) > 4
        and not token.endswith(("ses", "xes", "zes", "ches", "shes"))
    ):
        return token[:-1]
    return token


def slugify(raw: str) -> str:
    """Normalize Unicode, lowercase, and collapse non-alphanumeric runs to single hyphens."""
    normalized = unicodedata.normalize("NFKD", raw)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")


def normalize_author(raw: str | None) -> AuthorNode:
    """Deterministically normalize an author byline into an AuthorNode."""
    if not raw or not raw.strip():
        return AuthorNode(slug="unknown-author", name="Unknown Author")
    cleaned_name = " ".join(raw.strip().split())
    slug = slugify(cleaned_name)
    if not slug:
        return AuthorNode(slug="unknown-author", name="Unknown Author")
    return AuthorNode(slug=slug, name=cleaned_name)


def normalize_topic(raw: str) -> ExtractedTopic | None:
    """Canonicalize a raw entity/topic string into an ExtractedTopic, or None if rejected."""
    if not raw or not raw.strip():
        return None

    cleaned = " ".join(unicodedata.normalize("NFKC", raw).strip().split())
    lowered = cleaned.lower().strip(".,;:!?'\"()[]{}")
    if not lowered:
        return None

    if lowered in TOPIC_ALIASES:
        slug, name = TOPIC_ALIASES[lowered]
        return ExtractedTopic(slug=slug, name=name)

    raw_slug = slugify(lowered)
    if not raw_slug or len(raw_slug) < 2:
        return None

    if raw_slug in TOPIC_ALIASES:
        slug, name = TOPIC_ALIASES[raw_slug]
        return ExtractedTopic(slug=slug, name=name)

    if raw_slug in STOP_TOPICS or lowered in STOP_TOPICS:
        return None

    # De-pluralize words in the slug (especially the head noun at the end)
    parts = raw_slug.split("-")
    parts[-1] = _depluralize_token(parts[-1])
    canonical_slug = "-".join(p for p in parts if p)

    if not canonical_slug or len(canonical_slug) < 2:
        return None

    if canonical_slug in TOPIC_ALIASES:
        slug, name = TOPIC_ALIASES[canonical_slug]
        return ExtractedTopic(slug=slug, name=name)

    if canonical_slug in STOP_TOPICS:
        return None

    # Build a clean display name
    acronyms = {"rag", "llm", "mcp", "rrf", "bm25", "sql", "api", "gpu", "cpu"}
    display_words = [
        w.upper() if w in acronyms else w.capitalize() for w in canonical_slug.split("-")
    ]
    display_name = " ".join(display_words)
    return ExtractedTopic(slug=canonical_slug, name=display_name)


def normalize_topics(raw_entities: list[str]) -> list[ExtractedTopic]:
    """Normalize and deduplicate a list of raw topic strings, preserving order."""
    seen_slugs: set[str] = set()
    topics: list[ExtractedTopic] = []
    for raw in raw_entities:
        topic = normalize_topic(raw)
        if topic is not None and topic.slug not in seen_slugs:
            seen_slugs.add(topic.slug)
            topics.append(topic)
    return topics

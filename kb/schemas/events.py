"""Event names and payloads — the contract between functions.

Every event has a name constant and a Pydantic model. Producers build the
model, `.model_dump(mode="json")` it into `inngest.Event(data=...)`, and
consumers validate with `Model.model_validate(ctx.event.data)` on the first
line of the handler."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

PUBLICATION_ADDED = "kb/publication.added"
ARTICLE_DISCOVERED = "kb/article.discovered"
ARTICLE_INGESTED = "kb/article.ingested"
ARTICLE_UPDATED = "kb/article.updated"
CLAIMS_EXTRACTED = "kb/claims.extracted"
ANSWER_PRODUCED = "kb/answer.produced"

DiscoverySource = Literal["rss", "backfill", "manual", "poll"]


class PublicationAdded(BaseModel):
    """FastAPI endpoint -> discovery/backfill. Kicks off ingestion for one publication."""

    feed_url: str
    name: str | None = None


class ArticleDiscovered(BaseModel):
    """discovery/backfill/freshness -> ingest. One candidate article to fetch."""

    canonical_id: str
    publication_id: UUID
    url: str
    title: str
    published_at: datetime
    via: DiscoverySource


class ArticleIngested(BaseModel):
    """ingest -> anyone listening. An article's passages are now persisted."""

    canonical_id: str
    article_id: UUID
    content_hash: str
    passage_count: int


class ArticleUpdated(BaseModel):
    """ingest -> anyone listening. An article's passages were superseded and updated."""

    canonical_id: str
    article_id: UUID
    content_hash: str
    passage_count: int


class ClaimsExtracted(BaseModel):
    """enrich -> downstream listeners / telemetry. Summarizes claim extraction
    and quote guardrail.
    """

    article_id: UUID
    canonical_id: str
    claim_count: int
    dropped_spans: int


class AnswerProduced(BaseModel):
    """POST /ask (fire-and-forget) -> evaluators (week 5). Points at a stored snapshot."""

    snapshot_id: str
    retriever_variant: str


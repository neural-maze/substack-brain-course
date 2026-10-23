"""SQLAlchemy models.

Only one function writes each table ("one writer per table"
rule): `add-publication` / `backfill-publication` write `publications` and
`jobs`, `ingest-article` writes `articles`/`passages`, the cost-capture
middleware writes `costs`, and `POST /ask` writes `snapshots`. Every pipeline
write is an upsert (snapshots are append-only); see `kb/db/queries.py`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from kb.config import settings


class Base(DeclarativeBase):
    pass


class Publication(Base):
    __tablename__ = "publications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    feed_url: Mapped[str] = mapped_column(String, nullable=False)
    homepage: Mapped[str] = mapped_column(String, nullable=False)
    active: Mapped[bool] = mapped_column(default=True, nullable=False)


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    canonical_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    publication_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("publications.id"), nullable=False
    )
    url: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    author: Mapped[str | None] = mapped_column(String, nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="ingested")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    passages: Mapped[list[Passage]] = relationship(back_populates="article")


class Passage(Base):
    __tablename__ = "passages"
    __table_args__ = (
        Index(
            "uq_passages_article_chunk",
            "article_id",
            "chunk_index",
            unique=True,
            postgresql_where=text("valid_to IS NULL"),
        ),
        Index("ix_passages_tsv", "tsv", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    article_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("articles.id"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(settings.embedding_dimensions), nullable=True
    )
    # Generated column (STORED) — written by hand in the migration, not autogenerate.
    tsv: Mapped[str] = mapped_column(TSVECTOR, nullable=True)
    # Temporal validity: a passage with `valid_to` set is no longer current.
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    article: Mapped[Article] = relationship(back_populates="passages")


class Cursor(Base):
    __tablename__ = "cursors"

    publication_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("publications.id"), primary_key=True
    )
    etag: Mapped[str | None] = mapped_column(String, nullable=True)
    last_modified: Mapped[str | None] = mapped_column(String, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    poll_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    not_modified_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Job(Base):
    __tablename__ = "jobs"

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Lets `GET /jobs/{event_id}` resolve "articles ingested so far" without
    # threading a job id through every ArticleDiscovered event.
    publication_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("publications.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Cost(Base):
    __tablename__ = "costs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    article_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("articles.id"), nullable=True
    )
    model: Mapped[str] = mapped_column(String, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    est_usd: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False, default=0)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Snapshot(Base):
    """One row per answer from `POST /ask`: what was asked, what was answered, and
    EVERY candidate passage the retriever surfaced (not only the cited ones), with
    its rank in each search engine. That is what lets us tell, later, whether a bad
    answer came from ingestion, retrieval or generation.
    """

    __tablename__ = "snapshots"

    snapshot_id: Mapped[str] = mapped_column(String, primary_key=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)
    citations: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False, default=list)
    retrieved: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False, default=list)
    retriever_variant: Mapped[str] = mapped_column(String, nullable=False)
    as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Filled in by the evaluators in week 5; always NULL in week 2.
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Failure(Base):
    """Accumulates execution failures from `inngest/function.failed` system events.
    Provides a real-world dataset of production errors for week 5 automated evals.
    """

    __tablename__ = "failures"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    function_id: Mapped[str] = mapped_column(String, nullable=False)
    run_id: Mapped[str] = mapped_column(String, nullable=False)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    event_data: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Claim(Base):
    """An attributed assertion extracted from an article, verified against a
    verbatim quote_span in the article text. Temporal validity mirrors Passage:
    active claims have `valid_to IS NULL`; superseded claims retain history.
    """

    __tablename__ = "claims"
    __table_args__ = (
        Index(
            "ix_claims_article_active",
            "article_id",
            postgresql_where=text("valid_to IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    article_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("articles.id"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    quote_span: Mapped[str] = mapped_column(Text, nullable=False)
    claim_type: Mapped[str] = mapped_column(
        String, nullable=False
    )  # opinion | measurement | prediction
    entities: Mapped[list[dict[str, str]]] = mapped_column(JSONB, nullable=False, default=list)
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False, default=1.0)
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id"), nullable=True
    )


class EvalResult(Base):
    """One row per evaluated `Snapshot`, written by `evaluate-answer` (Week 5).
    Records both deterministic code assertions and narrow binary LLM judge verdicts.
    """

    __tablename__ = "eval_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[str] = mapped_column(
        String, ForeignKey("snapshots.snapshot_id"), unique=True, nullable=False
    )
    deterministic_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    checks: Mapped[dict[str, bool]] = mapped_column(JSONB, nullable=False, default=dict)
    faithfulness_verdict: Mapped[str] = mapped_column(String, nullable=False)
    faithfulness_critique: Mapped[str] = mapped_column(Text, nullable=False)
    disagreement_verdict: Mapped[str] = mapped_column(String, nullable=False)
    disagreement_critique: Mapped[str] = mapped_column(Text, nullable=False)
    failure_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    composite_score: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EvalRun(Base):
    """One row per benchmark experiment or failure replay run (`run-eval-suite`)."""

    __tablename__ = "eval_runs"

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    suite_name: Mapped[str] = mapped_column(String, nullable=False)
    variants_summary: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    case_results: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )




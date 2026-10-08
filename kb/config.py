"""Every tunable number in the pipeline lives here. Never a magic number inline."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- External services ---
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    database_url: str = (
        "postgresql+asyncpg://substack_brain:substack_brain_pw@localhost:5433/substack_brain"
    )
    inngest_dev: bool = True
    inngest_base_url: str = "http://localhost:8288"
    lab_port: int = 8000

    # --- Fetching ---
    user_agent: str = "substack-brain/0.1 (+https://github.com/neural-maze/substack-brain-course)"
    request_timeout_s: float = 15.0
    max_article_bytes: int = 5_000_000

    # --- Chunking ---
    chunk_size: int = 1_000
    chunk_overlap: int = 150

    # --- Embeddings ---
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    embedding_batch_size: int = 96
    # Verified 2026-09-19 against OpenAI's pricing page; no output-token cost for embeddings.
    embedding_price_per_million_tokens: float = 0.02

    # --- Discovery & backfill ---
    discovery_limit: int = 5
    publications_file: str = "publications.yaml"
    # backfill-publication sends kb/article.discovered events in batches of this size.
    backfill_batch_size: int = 25

    # --- Flow control for ingest-article (week 2) ---
    # How many articles from the SAME publication can be in flight at once.
    concurrency_per_publication: int = 3
    # How many ingest-article runs may START per minute, across all publications.
    # embed_texts sends a whole article in one request (up to 96 chunks), so this
    # is roughly "embedding requests per minute". Keep it under your OpenAI quota:
    # POST /ask also embeds every question.
    ingest_runs_per_minute: int = 50
    # Inngest priority is a time shift in SECONDS (allowed range -600..600), not a
    # rank: a manual run is scheduled as if it had arrived 600s earlier, and a
    # backfill run as if it had arrived 600s later.
    priority_manual_s: int = 600
    priority_backfill_s: int = -600

    # --- Retrieval & answers (week 2) ---
    rrf_k: int = 60
    retrieval_limit: int = 5
    synthesis_model: str = "claude-sonnet-5-5"
    synthesis_max_tokens: int = 1024


settings = Settings()

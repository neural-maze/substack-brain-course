"""Every tunable number in the pipeline lives here. Never a magic number inline."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- External services ---
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    database_url: str = "postgresql+asyncpg://substack_brain:substack_brain_pw@localhost:5433/substack_brain"
    inngest_dev: bool = True
    inngest_base_url: str = "http://localhost:8288"
    lab_port: int = 8000

    # --- Fetching ---
    user_agent: str = "living-kb/0.1 (+https://github.com/neural-maze/substack-brain-course)"
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

    # --- Discovery ---
    discovery_limit: int = 5
    publications_file: str = "publications.yaml"


settings = Settings()

"""OpenAI embeddings, batched into as few provider calls as possible.

Returns a JSON-serializable dict carrying a top-level `"usage"` key — the
convention `CostCaptureMiddleware` (`kb/inngest_client.py`) recognizes to
record cost into the `costs` table, without every embedding/LLM call site
having to write to the database itself.
"""

from __future__ import annotations

from openai import AsyncOpenAI

from kb.config import settings

_client = AsyncOpenAI(api_key=settings.openai_api_key)


async def embed_texts(texts: list[str]) -> dict:
    if not texts:
        return {
            "embeddings": [],
            "usage": {
                "model": settings.embedding_model,
                "input_tokens": 0,
                "output_tokens": 0,
                "est_usd": 0.0,
            },
        }

    embeddings: list[list[float]] = []
    total_tokens = 0
    for start in range(0, len(texts), settings.embedding_batch_size):
        batch = texts[start : start + settings.embedding_batch_size]
        response = await _client.embeddings.create(
            input=batch,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
        )
        # The API does not guarantee response order matches input order.
        ordered = sorted(response.data, key=lambda item: item.index)
        embeddings.extend(item.embedding for item in ordered)
        total_tokens += response.usage.total_tokens

    est_usd = (total_tokens / 1_000_000) * settings.embedding_price_per_million_tokens
    return {
        "embeddings": embeddings,
        "usage": {
            "model": settings.embedding_model,
            "input_tokens": total_tokens,
            "output_tokens": 0,
            "est_usd": round(est_usd, 6),
        },
    }

"""Dense (pgvector cosine-similarity) retrieval, called directly by `GET /search`."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from kb.db.queries import search_passages_dense
from kb.sources.embedder import embed_texts


async def search(session: AsyncSession, query: str, limit: int) -> list[dict[str, object]]:
    embedded = await embed_texts([query])
    (query_embedding,) = embedded["embeddings"]
    return await search_passages_dense(session, query_embedding, limit)

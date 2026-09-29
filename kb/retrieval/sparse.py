"""Sparse (BM25) retrieval, called directly by `GET /search`.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from kb.db.queries import search_passages_bm25


async def search(session: AsyncSession, query: str, limit: int) -> list[dict[str, object]]:
    return await search_passages_bm25(session, query, limit)

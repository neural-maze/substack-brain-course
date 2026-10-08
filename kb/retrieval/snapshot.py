"""Context snapshots: persist everything about one answer from `POST /ask`.

Every candidate the retriever surfaced is stored (with its rank in each
engine), not only the passages the model cited. With only the cited ones you
cannot tell "the right passage was never retrieved" from "it was retrieved
and the model ignored it", and those two problems have different fixes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from kb.db.queries import save_snapshot
from kb.schemas.ids import snapshot_id


async def record_snapshot(
    session: AsyncSession,
    *,
    question: str,
    answer_text: str,
    citations: list[dict[str, Any]],
    retrieved: list[dict[str, Any]],
    retriever_variant: str,
    as_of: datetime | None,
    latency_ms: int,
) -> str:
    sid = snapshot_id()
    await save_snapshot(
        session,
        snapshot_id=sid,
        question=question,
        answer_text=answer_text,
        citations=citations,
        retrieved=retrieved,
        retriever_variant=retriever_variant,
        as_of=as_of,
        latency_ms=latency_ms,
    )
    return sid

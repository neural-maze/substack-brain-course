"""POST /ask: answer a question from the knowledge base, with inline citations.

This is a fast read, so it never creates an Inngest run. A workflow engine is
for expensive work you'd hate to lose; here someone is waiting, and retrying
in three minutes is useless. So `ask_question` talks to Postgres and Claude
directly, saves a snapshot of everything it did, and only then sends a
fire-and-forget `kb/answer.produced` event for the evaluators of week 5.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime
from typing import Any

import anthropic
import inngest
from sqlalchemy.ext.asyncio import AsyncSession

from kb.config import settings
from kb.inngest_client import client
from kb.retrieval.hybrid import hybrid_search
from kb.retrieval.snapshot import record_snapshot
from kb.schemas.events import ANSWER_PRODUCED, AnswerProduced

logger = logging.getLogger(__name__)

_anthropic_client: anthropic.AsyncAnthropic | None = None
# asyncio only keeps weak references to tasks: a fire-and-forget task nobody
# holds on to can be garbage-collected before it finishes.
_background_tasks: set[asyncio.Task[None]] = set()

SYSTEM_PROMPT = (
    "You are a precise technical research assistant answering questions from curated AI "
    "engineering publications.\nFollow these rules strictly:\n"
    "1. Base your answer strictly on the provided context passages.\n"
    "2. Cite your sources inline using bracketed numbers like [1], [2] corresponding to "
    "the passage index.\n"
    "3. If the context is thin or missing, say clearly: 'Based on available publications, "
    "coverage of this topic is limited.'\n"
    "4. Do not speculate or extrapolate beyond what the passages state."
)


def get_anthropic_client() -> anthropic.AsyncAnthropic | None:
    global _anthropic_client
    if not settings.anthropic_api_key:
        return None
    if _anthropic_client is None:
        _anthropic_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _anthropic_client


def _format_context(passages: list[dict[str, Any]]) -> str:
    parts = []
    for idx, p in enumerate(passages, start=1):
        header = (
            f"[{idx}] (Title: {p.get('title', 'Unknown')} | "
            f"Author: {p.get('author') or 'Unknown'} | URL: {p.get('url')})"
        )
        parts.append(f"{header}\n{p.get('text', '')}")
    return "\n\n".join(parts)


def _citation(idx: int, passage: dict[str, Any]) -> dict[str, Any]:
    return {
        "citation_idx": idx,
        "passage_id": passage["passage_id"],
        "title": passage["title"],
        "author": passage["author"],
        "url": passage["url"],
    }


def _newest_date(passages: list[dict[str, Any]]) -> datetime | None:
    dates: list[datetime] = []
    for p in passages:
        raw = p.get("published_at")
        if raw:
            with contextlib.suppress(ValueError):
                dates.append(datetime.fromisoformat(str(raw)))
    return max(dates) if dates else None


async def _synthesize(
    llm: anthropic.AsyncAnthropic, question: str, passages: list[dict[str, Any]]
) -> str:
    prompt = (
        f"Context passages:\n{_format_context(passages)}\n\n"
        f"Question: {question}\n\n"
        "Answer with inline citations:"
    )
    try:
        response = await llm.messages.create(
            model=settings.synthesis_model,
            max_tokens=settings.synthesis_max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        logger.exception("LLM synthesis failed")
        return f"LLM synthesis failed ({type(exc).__name__}): {exc}"
    texts = [
        block.text
        for block in response.content
        if getattr(block, "type", None) == "text" and hasattr(block, "text")
    ]
    return "\n\n".join(texts)


async def _emit_answer_produced(snapshot_id: str, retriever_variant: str) -> None:
    try:
        await client.send(
            inngest.Event(
                name=ANSWER_PRODUCED,
                data=AnswerProduced(
                    snapshot_id=snapshot_id, retriever_variant=retriever_variant
                ).model_dump(mode="json"),
            )
        )
    except Exception:
        # Telemetry must never fail the user's request, but it shouldn't fail
        # silently either. The snapshot is already committed, so nothing is lost.
        logger.exception("Failed to emit %s for %s", ANSWER_PRODUCED, snapshot_id)


async def ask_question(
    session: AsyncSession, question: str, limit: int | None = None
) -> dict[str, Any]:
    start_time = time.perf_counter()
    retriever_variant = f"hybrid_rrf_k{settings.rrf_k}"

    # 1. Hybrid retrieval: the top passages go into the prompt, ALL candidates
    #    go into the snapshot.
    top_passages, all_candidates = await hybrid_search(
        session, query=question, limit=limit or settings.retrieval_limit
    )

    # 2. Answer with Claude, citing passages as [1], [2], ...
    llm = get_anthropic_client()
    if not top_passages:
        answer_text = "No relevant passages were found in the knowledge base for this question."
        citations: list[dict[str, Any]] = []
    elif llm is None:
        answer_text = (
            f"Retrieved {len(top_passages)} relevant passages with hybrid search. "
            "Set ANTHROPIC_API_KEY to get an answer with inline citations."
        )
        citations = [_citation(i, p) for i, p in enumerate(top_passages, start=1)]
    else:
        answer_text = await _synthesize(llm, question, top_passages)
        # Only the passages the model actually cited become citations.
        citations = [
            _citation(i, p) for i, p in enumerate(top_passages, start=1) if f"[{i}]" in answer_text
        ]

    # 3. as_of: publication date of the newest CITED source (falls back to all
    #    retrieved passages, then to now), so readers know how fresh the answer is.
    cited_ids = {c["passage_id"] for c in citations}
    cited = [p for p in top_passages if p["passage_id"] in cited_ids]
    as_of = _newest_date(cited) or _newest_date(top_passages) or datetime.now(tz=UTC)

    latency_ms = int((time.perf_counter() - start_time) * 1000)

    # 4. Snapshot first, and committed, BEFORE anything is sent anywhere.
    snapshot_id = await record_snapshot(
        session,
        question=question,
        answer_text=answer_text,
        citations=citations,
        retrieved=all_candidates,
        retriever_variant=retriever_variant,
        as_of=as_of,
        latency_ms=latency_ms,
    )
    await session.commit()

    # 5. Fire-and-forget: the user doesn't wait for the event to be delivered.
    task = asyncio.create_task(_emit_answer_produced(snapshot_id, retriever_variant))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return {
        "answer": answer_text,
        "citations": citations,
        "as_of": as_of.isoformat(),
        "snapshot_id": snapshot_id,
        "latency_ms": latency_ms,
    }

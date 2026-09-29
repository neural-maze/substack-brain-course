"""Every SQL statement in the project lives here. Functions and FastAPI endpoints
(MCP tools from week 3 on) call these helpers; they never build SQL themselves
(substack-brain-python skill).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from kb.db.models import Article, Cost, Job, Passage, Publication


async def upsert_publication(
    session: AsyncSession, *, slug: str, name: str, feed_url: str, homepage: str
) -> uuid.UUID:
    stmt = (
        insert(Publication)
        .values(id=uuid.uuid4(), slug=slug, name=name, feed_url=feed_url, homepage=homepage)
        .on_conflict_do_update(
            index_elements=[Publication.slug],
            set_={"name": name, "feed_url": feed_url, "homepage": homepage},
        )
        .returning(Publication.id)
    )
    return (await session.execute(stmt)).scalar_one()


async def get_publication_by_slug(session: AsyncSession, slug: str) -> Publication | None:
    return (
        await session.execute(select(Publication).where(Publication.slug == slug))
    ).scalar_one_or_none()


async def list_publications(session: AsyncSession) -> list[Publication]:
    return list((await session.execute(select(Publication))).scalars())


async def upsert_article(
    session: AsyncSession,
    *,
    canonical_id: str,
    publication_id: uuid.UUID,
    url: str,
    title: str,
    author: str | None,
    published_at: datetime,
    content_hash: str,
    text: str,
) -> uuid.UUID:
    """Upsert by `canonical_id` — the dedupe key across every ingestion path."""
    stmt = (
        insert(Article)
        .values(
            id=uuid.uuid4(),
            canonical_id=canonical_id,
            publication_id=publication_id,
            url=url,
            title=title,
            author=author,
            published_at=published_at,
            content_hash=content_hash,
            text=text,
        )
        .on_conflict_do_update(
            index_elements=[Article.canonical_id],
            set_={
                "title": title,
                "author": author,
                "published_at": published_at,
                "content_hash": content_hash,
                "text": text,
                "updated_at": func.now(),
            },
        )
        .returning(Article.id)
    )
    return (await session.execute(stmt)).scalar_one()


async def replace_passages(
    session: AsyncSession,
    *,
    article_id: uuid.UUID,
    passages: list[dict[str, object]],
) -> int:
    """Upsert by `(article_id, chunk_index)` — re-running produces zero new rows.

    `tsv` is a generated column (see the migration) and is never written here.
    """
    if not passages:
        return 0
    stmt = insert(Passage).values(
        [
            {
                "id": uuid.uuid4(),
                "article_id": article_id,
                "chunk_index": p["chunk_index"],
                "text": p["text"],
                "embedding": p["embedding"],
            }
            for p in passages
        ]
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[Passage.article_id, Passage.chunk_index],
        set_={"text": stmt.excluded.text, "embedding": stmt.excluded.embedding},
    )
    await session.execute(stmt)
    return len(passages)


async def create_job(
    session: AsyncSession,
    *,
    event_id: str,
    kind: str,
    total: int,
    publication_id: uuid.UUID | None = None,
) -> None:
    stmt = (
        insert(Job)
        .values(
            event_id=event_id,
            kind=kind,
            status="running",
            total=total,
            done=0,
            publication_id=publication_id,
        )
        .on_conflict_do_nothing(index_elements=[Job.event_id])
    )
    await session.execute(stmt)


async def get_job(session: AsyncSession, event_id: str) -> Job | None:
    return (await session.execute(select(Job).where(Job.event_id == event_id))).scalar_one_or_none()


async def get_job_status(session: AsyncSession, event_id: str) -> dict[str, object] | None:
    """`done` is resolved from real article rows, not incremented by ingest-article
    (which never learns which discovery job triggered it — see the Job model's
    `publication_id` comment). Read-repairs `jobs.done` in the same query.
    """
    job = await get_job(session, event_id)
    if job is None:
        return None
    done = job.total if job.publication_id is None else min(
        await count_articles_for_publication(session, job.publication_id), job.total
    )
    if done != job.done:
        job.done = done
    if done >= job.total and job.status != "completed":
        job.status = "completed"
    return {
        "event_id": job.event_id,
        "kind": job.kind,
        "status": job.status,
        "total": job.total,
        "done": job.done,
    }


async def count_articles(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(Article))).scalar_one()


async def count_passages(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(Passage))).scalar_one()


async def count_articles_for_publication(session: AsyncSession, publication_id: uuid.UUID) -> int:
    stmt = select(func.count()).select_from(Article).where(Article.publication_id == publication_id)
    return (await session.execute(stmt)).scalar_one()


async def insert_cost(
    session: AsyncSession,
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    est_usd: float,
    article_id: uuid.UUID | None = None,
) -> None:
    session.add(
        Cost(
            id=uuid.uuid4(),
            article_id=article_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            est_usd=est_usd,
        )
    )


async def search_passages_bm25(
    session: AsyncSession, query: str, limit: int
) -> list[dict[str, object]]:
    """BM25 (tsvector) search, no LLM. Sparse half of the week-1 retrieval preview
    (`kb/retrieval/sparse.py`); fused with the dense half from week 2 on.
    """
    tsquery = func.websearch_to_tsquery("english", query)
    rank = func.ts_rank(Passage.tsv, tsquery)
    stmt = (
        select(
            Passage.id,
            Passage.text,
            Article.title,
            Article.author,
            Article.url,
            Article.published_at,
            rank.label("rank"),
        )
        .join(Article, Article.id == Passage.article_id)
        .where(Passage.tsv.op("@@")(tsquery), Passage.valid_to.is_(None))
        .order_by(rank.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "passage_id": str(row.id),
            "text": row.text,
            "title": row.title,
            "author": row.author,
            "url": row.url,
            "published_at": row.published_at.isoformat(),
            "rank": float(row.rank),
        }
        for row in rows
    ]


async def search_passages_dense(
    session: AsyncSession, query_embedding: list[float], limit: int
) -> list[dict[str, object]]:
    """Cosine-similarity search over `passages.embedding` (pgvector), no LLM.
    Dense half of the week-1 retrieval preview (`kb/retrieval/dense.py`); fused
    with the sparse half from week 2 on.
    """
    distance = Passage.embedding.cosine_distance(query_embedding)
    stmt = (
        select(
            Passage.id,
            Passage.text,
            Article.title,
            Article.author,
            Article.url,
            Article.published_at,
            distance.label("distance"),
        )
        .join(Article, Article.id == Passage.article_id)
        .where(Passage.valid_to.is_(None), Passage.embedding.is_not(None))
        .order_by(distance.asc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "passage_id": str(row.id),
            "text": row.text,
            "title": row.title,
            "author": row.author,
            "url": row.url,
            "published_at": row.published_at.isoformat(),
            # Cosine similarity, not distance — higher is more relevant, consistent
            # with search_passages_bm25's `rank` so callers don't special-case sign.
            "rank": 1.0 - float(row.distance),
        }
        for row in rows
    ]

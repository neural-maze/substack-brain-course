"""Every SQL statement in the project lives here. Functions and FastAPI endpoints
call these helpers; they never build SQL themselves.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from kb.db.models import (
    Article,
    Claim,
    Cost,
    Cursor,
    Failure,
    Job,
    Passage,
    Publication,
    Snapshot,
)


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


async def get_existing_canonical_ids(session: AsyncSession, canonical_ids: list[str]) -> set[str]:
    """Which of these articles are already stored?"""
    stmt = select(Article.canonical_id).where(Article.canonical_id.in_(canonical_ids))
    return set((await session.execute(stmt)).scalars())


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
        index_where=Passage.valid_to.is_(None),
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

    Only articles written since the job started count towards it: a backfill of a
    publication that already has articles must not look finished before it starts.
    """
    job = await get_job(session, event_id)
    if job is None:
        return None
    done = (
        job.total
        if job.publication_id is None
        else min(
            await count_articles_for_publication_since(session, job.publication_id, job.created_at),
            job.total,
        )
    )
    done = max(done, job.done)
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


async def count_articles_for_publication_since(
    session: AsyncSession, publication_id: uuid.UUID, since: datetime
) -> int:
    """Articles of a publication created or updated at or after `since`."""
    stmt = (
        select(func.count())
        .select_from(Article)
        .where(Article.publication_id == publication_id, Article.updated_at >= since)
    )
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


async def save_snapshot(
    session: AsyncSession,
    *,
    snapshot_id: str,
    question: str,
    answer_text: str,
    citations: list[dict[str, object]],
    retrieved: list[dict[str, object]],
    retriever_variant: str,
    as_of: datetime | None,
    latency_ms: int,
) -> None:
    session.add(
        Snapshot(
            snapshot_id=snapshot_id,
            question=question,
            answer_text=answer_text,
            citations=citations,
            retrieved=retrieved,
            retriever_variant=retriever_variant,
            as_of=as_of,
            latency_ms=latency_ms,
        )
    )


async def get_snapshot(session: AsyncSession, snapshot_id: str) -> Snapshot | None:
    stmt = select(Snapshot).where(Snapshot.snapshot_id == snapshot_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_article_by_canonical_id(session: AsyncSession, canonical_id: str) -> Article | None:
    stmt = select(Article).where(Article.canonical_id == canonical_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def supersede_passages(session: AsyncSession, article_id: uuid.UUID) -> int:
    """Close active passages by setting valid_to = now(). History is preserved."""
    stmt = (
        update(Passage)
        .where(Passage.article_id == article_id, Passage.valid_to.is_(None))
        .values(valid_to=func.now())
    )
    result = await session.execute(stmt)
    return getattr(result, "rowcount", 0)


async def insert_passages_fresh(
    session: AsyncSession,
    *,
    article_id: uuid.UUID,
    passages: list[dict[str, object]],
) -> int:
    """Insert new passage versions without conflicting on previously superseded rows."""
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
    await session.execute(stmt)
    return len(passages)


async def get_cursor(session: AsyncSession, publication_id: uuid.UUID) -> Cursor | None:
    stmt = select(Cursor).where(Cursor.publication_id == publication_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def update_cursor(
    session: AsyncSession,
    *,
    publication_id: uuid.UUID,
    etag: str | None,
    last_modified: str | None,
    not_modified: bool = False,
) -> None:
    """Update or insert publication polling cursor with conditional response metadata."""
    existing = await get_cursor(session, publication_id)
    if existing is None:
        cursor = Cursor(
            publication_id=publication_id,
            etag=etag,
            last_modified=last_modified,
            last_checked_at=datetime.now(),
            poll_count=1,
            not_modified_count=1 if not_modified else 0,
        )
        session.add(cursor)
    else:
        existing.poll_count += 1
        if not_modified:
            existing.not_modified_count += 1
        else:
            if etag:
                existing.etag = etag
            if last_modified:
                existing.last_modified = last_modified
        existing.last_checked_at = datetime.now()


async def record_failure(
    session: AsyncSession,
    *,
    function_id: str,
    run_id: str,
    error: str,
    event_data: dict[str, object] | None = None,
) -> uuid.UUID:
    failure = Failure(
        id=uuid.uuid4(),
        function_id=function_id,
        run_id=run_id,
        error=error,
        event_data=event_data,
    )
    session.add(failure)
    await session.flush()
    return failure.id


async def list_publications_with_stats(session: AsyncSession) -> list[dict[str, object]]:
    """List publications alongside article counts and cursor sync info."""
    pubs = await list_publications(session)
    results = []
    for pub in pubs:
        art_count = await count_articles_for_publication(session, pub.id)
        cursor = await get_cursor(session, pub.id)
        last_checked = (
            cursor.last_checked_at.isoformat() if cursor and cursor.last_checked_at else None
        )
        results.append(
            {
                "id": str(pub.id),
                "slug": pub.slug,
                "name": pub.name,
                "feed_url": pub.feed_url,
                "homepage": pub.homepage,
                "active": pub.active,
                "article_count": art_count,
                "last_checked_at": last_checked,
                "poll_count": cursor.poll_count if cursor else 0,
                "not_modified_count": cursor.not_modified_count if cursor else 0,
            }
        )
    return results


async def get_article_with_publication(
    session: AsyncSession, article_id: uuid.UUID
) -> tuple[Article, Publication] | None:
    """Fetch an Article alongside its parent Publication."""
    stmt = (
        select(Article, Publication)
        .join(Publication, Publication.id == Article.publication_id)
        .where(Article.id == article_id)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    return row[0], row[1]


async def count_active_claims(session: AsyncSession) -> int:
    """Count active claims (`valid_to IS NULL`) in Postgres."""
    stmt = select(func.count()).select_from(Claim).where(Claim.valid_to.is_(None))
    return (await session.execute(stmt)).scalar_one()


async def get_active_claims_for_article(
    session: AsyncSession, article_id: uuid.UUID
) -> list[Claim]:
    """Return all currently active claims for an article."""
    stmt = select(Claim).where(Claim.article_id == article_id, Claim.valid_to.is_(None))
    return list((await session.execute(stmt)).scalars())


async def persist_article_claims(
    session: AsyncSession,
    *,
    article_id: uuid.UUID,
    claims: list[dict[str, object]],
    now: datetime,
) -> list[dict[str, str]]:
    """Supersede stale active claims for `article_id` and upsert the newly validated
    claims. Returns `supersedes_pairs` (`[{new_claim_id, old_claim_id, at}]`) so
    Memgraph can merge `[:SUPERSEDES]` edges for topic-overlapping revisions.
    """
    existing_active = await get_active_claims_for_article(session, article_id)
    incoming_ids = {uuid.UUID(str(c["claim_id"])) for c in claims}

    # Any active claim not in the new batch belongs to a prior revision and is superseded
    stale_claims = [old for old in existing_active if old.id not in incoming_ids]
    if stale_claims:
        stale_ids = [old.id for old in stale_claims]
        await session.execute(
            update(Claim).where(Claim.id.in_(stale_ids)).values(valid_to=now)
        )

    # Map topic slug -> most recent superseded claim id so new claims on the same
    # topic can link via supersedes_id / [:SUPERSEDES]
    topic_to_old_claim_id: dict[str, uuid.UUID] = {}
    for old in stale_claims:
        for ent in old.entities or []:
            if isinstance(ent, dict) and ent.get("slug"):
                topic_to_old_claim_id[str(ent["slug"])] = old.id

    supersedes_pairs: list[dict[str, str]] = []
    now_iso = now.isoformat()

    for c in claims:
        cid = uuid.UUID(str(c["claim_id"]))
        topics_list = c.get("topics", [])
        entities_json: list[dict[str, str]] = (
            [
                {"slug": str(t["slug"]), "name": str(t["name"])}
                for t in topics_list
                if isinstance(t, dict) and "slug" in t and "name" in t
            ]
            if isinstance(topics_list, list)
            else []
        )

        supersedes_uuid: uuid.UUID | None = None
        for ent in entities_json:
            old_id = topic_to_old_claim_id.get(ent["slug"])
            if old_id is not None and old_id != cid:
                supersedes_uuid = old_id
                supersedes_pairs.append(
                    {
                        "new_claim_id": str(cid),
                        "old_claim_id": str(old_id),
                        "at": now_iso,
                    }
                )
                break

        stmt = (
            insert(Claim)
            .values(
                id=cid,
                article_id=article_id,
                text=str(c["text"]),
                quote_span=str(c["quote_span"]),
                claim_type=str(c["claim_type"]),
                entities=entities_json,
                confidence=float(str(c.get("confidence", 0.9))),
                valid_from=now,
                valid_to=None,
                supersedes_id=supersedes_uuid,
            )
            .on_conflict_do_update(
                index_elements=[Claim.id],
                set_={
                    "text": str(c["text"]),
                    "quote_span": str(c["quote_span"]),
                    "claim_type": str(c["claim_type"]),
                    "entities": entities_json,
                    "confidence": float(str(c.get("confidence", 0.9))),
                    "valid_to": None,
                    "supersedes_id": supersedes_uuid,
                },
            )
        )
        await session.execute(stmt)

    return supersedes_pairs


async def find_matching_passage_for_quote(
    session: AsyncSession,
    *,
    article_id: uuid.UUID,
    quote_span: str,
) -> dict[str, object] | None:
    """Resolve an active Postgres passage from `article_id` containing `quote_span`
    (falling back to the first active passage of the article if the quote spans a
    chunk boundary).
    """
    stmt = (
        select(
            Passage.id,
            Passage.text,
            Passage.chunk_index,
            Article.title,
            Article.author,
            Article.url,
            Article.published_at,
        )
        .join(Article, Article.id == Passage.article_id)
        .where(Passage.article_id == article_id, Passage.valid_to.is_(None))
        .order_by(Passage.chunk_index.asc())
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        return None

    norm_quote = " ".join(quote_span.split()).strip().lower()
    probe = norm_quote[:60] if len(norm_quote) > 60 else norm_quote

    chosen = rows[0]
    for row in rows:
        norm_passage = " ".join(str(row.text).split()).strip().lower()
        if norm_quote in norm_passage or (probe and probe in norm_passage):
            chosen = row
            break

    return {
        "passage_id": str(chosen.id),
        "text": chosen.text,
        "title": chosen.title,
        "author": chosen.author,
        "url": chosen.url,
        "published_at": chosen.published_at.isoformat(),
    }


async def get_source_provenance(
    session: AsyncSession, target_id: uuid.UUID
) -> dict[str, object] | None:
    """Look up full lineage for a `claim_id` or `passage_id` for `kb_sources`."""
    claim_stmt = (
        select(Claim, Article, Publication)
        .join(Article, Article.id == Claim.article_id)
        .join(Publication, Publication.id == Article.publication_id)
        .where(Claim.id == target_id)
    )
    claim_row = (await session.execute(claim_stmt)).first()
    if claim_row is not None:
        claim, article, pub = claim_row
        return {
            "kind": "claim",
            "id": str(claim.id),
            "article": {
                "article_id": str(article.id),
                "canonical_id": article.canonical_id,
                "title": article.title,
                "url": article.url,
                "published_at": article.published_at.isoformat(),
                "content_hash": article.content_hash,
            },
            "publication": {
                "slug": pub.slug,
                "name": pub.name,
                "homepage": pub.homepage,
            },
            "author": article.author or "Unknown Author",
            "verbatim_quote": claim.quote_span,
            "claim_text": claim.text,
            "claim_type": claim.claim_type,
            "confidence": float(claim.confidence),
            "topics": list(claim.entities or []),
            "validity": {
                "valid_from": claim.valid_from.isoformat(),
                "valid_to": claim.valid_to.isoformat() if claim.valid_to else None,
                "is_active": claim.valid_to is None,
                "supersedes_id": str(claim.supersedes_id) if claim.supersedes_id else None,
            },
        }

    passage_stmt = (
        select(Passage, Article, Publication)
        .join(Article, Article.id == Passage.article_id)
        .join(Publication, Publication.id == Article.publication_id)
        .where(Passage.id == target_id)
    )
    passage_row = (await session.execute(passage_stmt)).first()
    if passage_row is not None:
        passage, article, pub = passage_row
        return {
            "kind": "passage",
            "id": str(passage.id),
            "chunk_index": passage.chunk_index,
            "article": {
                "article_id": str(article.id),
                "canonical_id": article.canonical_id,
                "title": article.title,
                "url": article.url,
                "published_at": article.published_at.isoformat(),
                "content_hash": article.content_hash,
            },
            "publication": {
                "slug": pub.slug,
                "name": pub.name,
                "homepage": pub.homepage,
            },
            "author": article.author or "Unknown Author",
            "verbatim_quote": passage.text,
            "validity": {
                "valid_from": passage.valid_from.isoformat(),
                "valid_to": passage.valid_to.isoformat() if passage.valid_to else None,
                "is_active": passage.valid_to is None,
                "supersedes_id": None,
            },
        }

    return None



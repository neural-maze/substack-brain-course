"""Replay a backfill and prove it writes nothing new.

    uv run python scripts/replay_backfill.py --verify            # print the counts
    uv run python scripts/replay_backfill.py --all               # replay every publication
    uv run python scripts/replay_backfill.py --slug decoding-ai  # replay one publication

`--all` / `--slug` re-send `kb/publication.added` for publications in
`publications.yaml`, exactly like `POST /publications` does. Run `--verify`
before and after (once Inngest has finished): if ingestion is idempotent, the
publication, article and passage counts are identical, and so is the number
of embedding calls recorded in `costs`, because already-stored articles never
reach the `embed` step.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import inngest
from sqlalchemy import func, select

from kb.config import settings
from kb.db.models import Article, Cost, Passage, Publication
from kb.db.session import get_session
from kb.inngest_client import client
from kb.schemas.events import PUBLICATION_ADDED, PublicationAdded
from kb.sources.allowlist import load_publications


async def counts() -> dict[str, int]:
    async with get_session() as session:

        async def count(stmt: object) -> int:
            return int((await session.execute(stmt)).scalar_one())  # type: ignore[call-overload]

        return {
            "publications": await count(select(func.count(Publication.id))),
            "articles": await count(select(func.count(Article.id))),
            "passages": await count(select(func.count(Passage.id))),
            "embedding_calls": await count(
                select(func.count(Cost.id)).where(Cost.model == settings.embedding_model)
            ),
        }


async def verify() -> None:
    print("  ".join(f"{name}={value}" for name, value in (await counts()).items()))


async def replay(slug: str | None) -> None:
    pubs = load_publications()
    if slug:
        pubs = [p for p in pubs if p.slug == slug]
        if not pubs:
            sys.exit(f"No publication with slug {slug!r} in publications.yaml")

    await verify()
    for pub in pubs:
        await client.send(
            inngest.Event(
                name=PUBLICATION_ADDED,
                data=PublicationAdded(feed_url=pub.feed_url, name=pub.name).model_dump(mode="json"),
            )
        )
        print(f"sent {PUBLICATION_ADDED} for {pub.slug}")
    print("Wait for the runs to finish in the Inngest UI, then run with --verify.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--verify", action="store_true", help="print the current counts")
    group.add_argument("--all", action="store_true", help="replay every publication")
    group.add_argument("--slug", help="replay a single publication")
    args = parser.parse_args()

    asyncio.run(verify() if args.verify else replay(args.slug))


if __name__ == "__main__":
    main()

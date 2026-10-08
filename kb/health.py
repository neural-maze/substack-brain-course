"""Reachability checks for `GET /health` (kb/main.py)."""

from __future__ import annotations

import httpx
from sqlalchemy import text

from kb.config import settings
from kb.db.queries import count_articles, count_passages
from kb.db.session import get_session


async def check_health() -> dict[str, object]:
    checks: dict[str, str] = {}

    # Each subsystem check is isolated and broad-caught on purpose: a health
    # check must report *which* dependency is down, not raise itself.
    try:
        async with get_session() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["database"] = f"error: {exc}"

    try:
        async with httpx.AsyncClient(timeout=3.0) as http_client:
            await http_client.get(settings.inngest_base_url)
        checks["inngest"] = "ok"
    except httpx.HTTPError as exc:
        checks["inngest"] = f"error: {exc}"

    articles = passages = 0
    if checks.get("database") == "ok":
        async with get_session() as session:
            articles = await count_articles(session)
            passages = await count_passages(session)

    overall_ok = all(v == "ok" for v in checks.values())
    return {
        "status": "ok" if overall_ok else "degraded",
        **checks,
        "articles": articles,
        "passages": passages,
    }

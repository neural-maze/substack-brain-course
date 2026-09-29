"""The Inngest client, plus middleware that captures LLM/embedding cost.

Any step that calls an LLM or embedding provider (e.g. `kb/sources/embedder.py`)
returns a JSON-serializable dict with a top-level `"usage"` key shaped like
`{"model", "input_tokens", "output_tokens", "est_usd"}`. `CostCaptureMiddleware`
recognizes that shape in `transform_output` and writes one row to `costs`,
without every call site needing to touch the database itself.

Why this doesn't double-count on replay: `transform_output` is invoked once per HTTP request from
Inngest to this app, and a request only reports a *newly executed* step —
already-completed steps are memoized and answered without re-running their
callable, so they never reach `transform_output` again. A genuine retry of a
failed step re-runs the callable (and the underlying LLM call) for real, so
recording its cost again is correct, not a bug.
"""

from __future__ import annotations

import typing
import uuid

import inngest

from kb.config import settings
from kb.db.queries import insert_cost
from kb.db.session import get_session


class UsageRecord(typing.TypedDict, total=False):
    model: str
    input_tokens: int
    output_tokens: int
    est_usd: float
    article_id: str | None


def _usage_from_output(output: object) -> UsageRecord | None:
    if not isinstance(output, dict):
        return None
    usage = output.get("usage")
    if not isinstance(usage, dict):
        return None
    if not {"model", "input_tokens", "output_tokens", "est_usd"} <= usage.keys():
        return None
    return typing.cast(UsageRecord, usage)


class CostCaptureMiddleware(inngest.Middleware):
    async def transform_output(self, result: inngest.TransformOutputResult) -> None:
        if result.step is None:
            return  # not a newly-executed step's output
        usage = _usage_from_output(result.output)
        if usage is None:
            return

        raw_article_id = usage.get("article_id")
        article_id = uuid.UUID(raw_article_id) if raw_article_id else None
        async with get_session() as session:
            await insert_cost(
                session,
                model=usage["model"],
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                est_usd=usage["est_usd"],
                article_id=article_id,
            )
            await session.commit()


client = inngest.Inngest(
    app_id="substack-brain",
    is_production=not settings.inngest_dev,
    middleware=[CostCaptureMiddleware],
)

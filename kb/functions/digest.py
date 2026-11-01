"""Week 6 automated weekly digest (`generate-weekly-digest`).

Dual-triggered by:
1. Cron (`settings.digest_cron`, default Mondays at 08:00 UTC: `"0 8 * * 1"`).
2. On-demand event (`kb/digest.requested` from `POST /digests/run`).

Executes 4 durable steps:
1. `collect-weekly-window`: Gather articles, active verified claims, and cross-publication
   `:Topic` hubs from the lookback window.
2. `synthesize-weekly-digest`: Write the structured weekly markdown briefing with inline
   `[k]` citations via `weekly_digest_agent` (or deterministic fallback when keyless).
3. `verify-and-persist-digest`: Run Level 1 deterministic citation checks (`FM-1`–`FM-3`)
   and upsert the row into `digests`.
4. `emit-digest-published`: Emit `kb/digest.published`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import inngest

from kb.agents.providers import call_llm
from kb.agents.roles.researcher import (
    extract_cited_entries,
    format_evidence_context,
    heuristic_synthesize_digest,
    weekly_digest_agent,
)
from kb.config import settings
from kb.db.queries import collect_weekly_digest_inputs, insert_digest
from kb.db.session import get_session
from kb.evals.deterministic import run_deterministic_checks
from kb.inngest_client import client
from kb.schemas.events import (
    DIGEST_PUBLISHED,
    DIGEST_REQUESTED,
    DigestPublished,
    DigestRequested,
)
from kb.schemas.ids import ARTICLE_CLAIM_NAMESPACE


@client.create_function(
    fn_id="generate-weekly-digest",
    name="Generate Weekly Knowledge Base Digest",
    trigger=[
        inngest.TriggerCron(cron=settings.digest_cron),
        inngest.TriggerEvent(event=DIGEST_REQUESTED),
    ],
    retries=2,
)
async def generate_weekly_digest(ctx: inngest.Context) -> dict[str, object]:
    raw_data = ctx.event.data or {}
    req = (
        DigestRequested.model_validate(raw_data)
        if raw_data
        else DigestRequested(lookback_days=settings.digest_lookback_days)
    )

    now = datetime.now(UTC)
    window_end = now.replace(minute=0, second=0, microsecond=0)
    window_start = window_end - timedelta(days=max(req.lookback_days, 1))
    title = (
        f"The Substack Brain Weekly Digest "
        f"({window_start.strftime('%Y-%m-%d')} to {window_end.strftime('%Y-%m-%d')})"
    )

    # Deterministic UUIDv5 per (window_start_date, window_end_date) so re-running the
    # same day's digest updates in place without duplicating rows.
    digest_uuid = uuid.uuid5(
        ARTICLE_CLAIM_NAMESPACE,
        f"digest:{window_start.date().isoformat()}:{window_end.date().isoformat()}",
    )

    async def _collect_window() -> dict[str, Any]:
        async with get_session() as session:
            return await collect_weekly_digest_inputs(
                session,
                window_start=window_start,
                window_end=window_end,
                publication_slugs=req.publication_slugs,
            )

    window_data = await ctx.step.run("collect-weekly-window", _collect_window)
    articles = cast("list[dict[str, Any]]", window_data["articles"])
    claims = cast("list[dict[str, Any]]", window_data["claims"])
    topics = cast("list[dict[str, Any]]", window_data["topics"])

    prompt_limit = 10
    top_claims = claims[:prompt_limit]
    cross_pub_topics = [t for t in topics if t.get("is_cross_publication")]

    async def _synthesize_digest() -> dict[str, Any]:
        if not top_claims:
            return {
                "output": heuristic_synthesize_digest(title, [], topics),
            }
        if settings.anthropic_api_key:
            topics_summary = "\n".join(
                f"- {t['name']} (publications: {', '.join(t.get('publications', []))}; "
                f"claims: {t['claim_count']})"
                for t in topics[:8]
            )
            prompt = (
                f"Digest Title: {title}\n"
                f"Articles Covered ({len(articles)}): "
                f"{', '.join(str(a['title']) for a in articles[:10])}\n\n"
                f"Canonical Topics This Week:\n{topics_summary or 'None'}\n\n"
                f"Numbered Claims & Verbatim Quotes:\n{format_evidence_context(top_claims)}\n\n"
                "Write the weekly markdown digest with inline [k] citations:"
            )
            return await call_llm(
                system_prompt=weekly_digest_agent.system_prompt,
                user_input=prompt,
                model=weekly_digest_agent.model,
                max_tokens=weekly_digest_agent.max_tokens,
                output_schema=None,
            )
        return {
            "output": heuristic_synthesize_digest(title, top_claims, topics),
        }

    synth_res = await ctx.step.run("synthesize-weekly-digest", _synthesize_digest)
    summary_markdown = str(
        synth_res.get("output", synth_res) if isinstance(synth_res, dict) else synth_res
    )
    citations = extract_cited_entries(summary_markdown, top_claims)

    async def _verify_and_persist() -> dict[str, Any]:
        det = run_deterministic_checks(
            answer_text=summary_markdown,
            citations=citations,
            retrieved=top_claims,
            prompt_limit=prompt_limit,
        )
        stats: dict[str, object] = {
            "articles_count": len(articles),
            "claims_count": len(claims),
            "topics_count": len(topics),
            "cross_pub_disagreements_count": len(cross_pub_topics),
            "top_topics": [str(t["name"]) for t in topics[:5]],
            "deterministic_passed": det.passed,
            "deterministic_checks": det.checks,
            "failure_codes": det.failure_codes,
        }
        async with get_session() as session:
            persisted_id = await insert_digest(
                session,
                digest_id=digest_uuid,
                window_start=window_start,
                window_end=window_end,
                title=title,
                summary_markdown=summary_markdown,
                citations=citations,
                stats=stats,
            )
            await session.commit()
        return {
            "digest_id": str(persisted_id),
            "title": title,
            "stats": stats,
        }

    persisted = await ctx.step.run("verify-and-persist-digest", _verify_and_persist)

    published_evt = DigestPublished(
        digest_id=digest_uuid,
        window_start=window_start,
        window_end=window_end,
        article_count=len(articles),
        claim_count=len(claims),
        disagreements_count=len(cross_pub_topics),
    )
    await ctx.step.send_event(
        "emit-digest-published",
        inngest.Event(
            name=DIGEST_PUBLISHED,
            data=published_evt.model_dump(mode="json"),
        ),
    )

    return persisted


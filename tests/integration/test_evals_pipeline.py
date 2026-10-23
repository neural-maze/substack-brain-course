"""Integration tests for Week 5 evaluation suite against real Postgres:

1. `upsert_eval_result` persists `EvalResult` and updates `snapshots.confidence` from
   `NULL` to the calibrated composite score.
2. `upsert_eval_run` persists multi-variant benchmark summaries (`eval_runs`).
3. `kb_eval_report` (10th MCP tool) and `get_latest_eval_report` return taxonomy counts,
   pass rates, recent snapshot critiques, and the latest variant experiment run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from kb.config import settings
from kb.db.queries import (
    get_latest_eval_report,
    get_snapshot,
    save_snapshot,
    upsert_eval_result,
    upsert_eval_run,
)
from kb.evals.metrics import evaluate_trace_offline, run_benchmark_variants
from kb.mcp import tools


@pytest.fixture(autouse=True)
def _require_dev_stack() -> None:
    try:
        with httpx.Client(timeout=2.0) as client:
            client.get(settings.inngest_base_url)
    except httpx.HTTPError:
        pytest.skip(
            f"Inngest Dev Server not reachable at {settings.inngest_base_url} — "
            "run `make start` first."
        )


async def test_eval_result_updates_snapshot_confidence_and_surfaces_in_mcp_report(
    db: AsyncSession,
) -> None:
    snap_pass_id = "snap_week5_pass_01"
    snap_fail_id = "snap_week5_fail_01"

    retrieved_sample: list[dict[str, object]] = [
        {
            "passage_id": "p-test-1",
            "title": "Durable Execution with Inngest",
            "author": "Miguel Otero Pedrido",
            "publication": "the-neural-maze",
            "url": "https://www.theneuralmaze.com/p/durable-harnesses",
            "published_at": "2026-10-01T00:00:00+00:00",
            "text": "Inngest memoizes every completed step output via ctx.step.run.",
            "claim_id": "c-test-1",
            "claim_text": "Inngest memoizes completed step outputs.",
            "claim_type": "measurement",
            "valid_to": None,
        }
    ]
    citations_sample: list[dict[str, object]] = [
        {
            "citation_idx": 1,
            "passage_id": "p-test-1",
            "title": "Durable Execution with Inngest",
            "author": "Miguel Otero Pedrido",
            "url": "https://www.theneuralmaze.com/p/durable-harnesses",
        }
    ]

    # 1. Save two snapshots (initially confidence=None)
    await save_snapshot(
        db,
        snapshot_id=snap_pass_id,
        question="How does Inngest memoize step outputs?",
        answer_text="Inngest memoizes every completed step output via ctx.step.run [1].",
        citations=citations_sample,
        retrieved=retrieved_sample,
        retriever_variant="hybrid_3way_rrf_k60",
        as_of=datetime(2026, 10, 1, tzinfo=UTC),
        latency_ms=120,
    )
    await save_snapshot(
        db,
        snapshot_id=snap_fail_id,
        question="How does Inngest memoize step outputs?",
        answer_text="Inngest memoizes steps [1] and uses a 9-node Raft cluster [5].",
        citations=citations_sample,
        retrieved=retrieved_sample,
        retriever_variant="hybrid_3way_rrf_k60",
        as_of=datetime(2026, 10, 1, tzinfo=UTC),
        latency_ms=145,
    )
    await db.flush()

    before = await get_snapshot(db, snap_pass_id)
    assert before is not None and before.confidence is None

    # 2. Evaluate both traces and persist to eval_results + snapshots.confidence
    pass_eval = evaluate_trace_offline(
        {
            "question": "How does Inngest memoize step outputs?",
            "retrieved": retrieved_sample,
            "citations": citations_sample,
            "answer_text": "Inngest memoizes every completed step output via ctx.step.run [1].",
        }
    )
    fail_eval = evaluate_trace_offline(
        {
            "question": "How does Inngest memoize step outputs?",
            "retrieved": retrieved_sample,
            "citations": citations_sample,
            "answer_text": "Inngest memoizes steps [1] and uses a 9-node Raft cluster [5].",
        }
    )

    await upsert_eval_result(
        db,
        snapshot_id=snap_pass_id,
        deterministic_passed=bool(pass_eval["deterministic_passed"]),
        checks=cast("dict[str, bool]", pass_eval["checks"]),
        faithfulness_verdict=str(pass_eval["faithfulness_verdict"]),
        faithfulness_critique=str(pass_eval["faithfulness_critique"]),
        disagreement_verdict=str(pass_eval["disagreement_verdict"]),
        disagreement_critique=str(pass_eval["disagreement_critique"]),
        failure_codes=cast("list[str]", pass_eval["failure_codes"]),
        composite_score=float(pass_eval["composite_score"]),
    )
    await upsert_eval_result(
        db,
        snapshot_id=snap_fail_id,
        deterministic_passed=bool(fail_eval["deterministic_passed"]),
        checks=cast("dict[str, bool]", fail_eval["checks"]),
        faithfulness_verdict=str(fail_eval["faithfulness_verdict"]),
        faithfulness_critique=str(fail_eval["faithfulness_critique"]),
        disagreement_verdict=str(fail_eval["disagreement_verdict"]),
        disagreement_critique=str(fail_eval["disagreement_critique"]),
        failure_codes=cast("list[str]", fail_eval["failure_codes"]),
        composite_score=float(fail_eval["composite_score"]),
    )

    # 3. Persist a benchmark variant suite run
    bench = run_benchmark_variants()
    await upsert_eval_run(
        db,
        run_id="eval_run_integration_test",
        suite_name=str(bench["suite_name"]),
        variants_summary=cast("dict[str, object]", bench["variants_summary"]),
        case_results=cast("list[dict[str, object]]", bench["case_results"]),
    )
    await db.commit()

    # 4. Verify snapshots.confidence was updated
    after_pass = await get_snapshot(db, snap_pass_id)
    after_fail = await get_snapshot(db, snap_fail_id)
    assert after_pass is not None and float(after_pass.confidence or 0.0) == 1.0
    assert after_fail is not None and float(after_fail.confidence or 0.0) == 0.0

    # 5. Verify kb_eval_report MCP tool returns taxonomy counts and suite summary
    report = await tools.kb_eval_report(limit=10)
    assert int(cast("int", report["total_evaluated_snapshots"])) >= 2
    tax_counts = cast("dict[str, int]", report["failure_taxonomy_counts"])
    assert tax_counts.get("FM-1", 0) >= 1

    latest_suite = cast("dict[str, Any]", report["latest_suite_run"])
    assert latest_suite is not None
    assert latest_suite["suite_name"] == "golden_v1"
    assert "hybrid_3way_rrf_k60" in latest_suite["variants_summary"]

    direct_report = await get_latest_eval_report(db, limit=10)
    assert direct_report["total_evaluated_snapshots"] == report["total_evaluated_snapshots"]

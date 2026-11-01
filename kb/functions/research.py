"""Week 6 bounded research agent (`run-research-agent`).

Triggered by `kb/research.requested`. Orchestrates a bounded multi-hop research
loop directly on Inngest durable steps (`ctx.step.run`):

1. `record-research-start`: Initialize the `research_runs` row with `status="running"`.
2. `plan-subqueries`: Decompose the user's question via `query_planner_agent` (or
   deterministic fallback when `ANTHROPIC_API_KEY` is unset).
3. `hop-{i}-retrieve` & `hop-{i}-reflect` (bounded by `max_iterations` and `max_usd`):
   - Run 3-way hybrid retrieval (`BM25 + pgvector + Memgraph`) across the hop's sub-queries.
   - Deduplicate candidates into the cumulative evidence pool (`merge_hop_candidates`).
   - Stop immediately if `iteration >= max_iterations`, `total_cost_usd >= max_usd`, or
     `newly_added == 0` (convergence guardrail).
   - Otherwise invoke `coverage_critic_agent` to decide whether coverage is sufficient
     or to emit `followup_queries` for the next hop.
4. `synthesize-report`: Synthesize a structured markdown report with inline `[k]` citations.
5. `evaluate-report`: Run Week 5's Level 1 deterministic checks (`FM-1`–`FM-3`) and
   Level 2 binary judges (`FM-4` faithfulness, `FM-5` disagreement coverage).
6. `persist-research-run`: Persist the completed `research_runs` row and emit
   `kb/research.completed`.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, cast

import inngest

from kb.agents.providers import call_llm
from kb.agents.roles.researcher import (
    CoverageReflection,
    ResearchPlan,
    coverage_critic_agent,
    extract_cited_entries,
    format_evidence_context,
    heuristic_plan_queries,
    heuristic_reflect_coverage,
    heuristic_synthesize_research,
    merge_hop_candidates,
    query_planner_agent,
    research_synthesizer_agent,
)
from kb.config import settings
from kb.db.queries import upsert_research_run
from kb.db.session import get_session
from kb.evals.deterministic import run_deterministic_checks
from kb.evals.judges import (
    BinaryVerdict,
    disagreement_judge_agent,
    faithfulness_judge_agent,
    format_judge_input,
    heuristic_judge_disagreement,
    heuristic_judge_faithfulness,
    requires_disagreement_check,
)
from kb.inngest_client import client
from kb.retrieval.hybrid import hybrid_search
from kb.schemas.events import (
    RESEARCH_COMPLETED,
    RESEARCH_REQUESTED,
    ResearchCompleted,
    ResearchRequested,
)


def _usage_usd(step_output: Any) -> float:
    if isinstance(step_output, dict):
        usage = step_output.get("usage")
        if isinstance(usage, dict) and "est_usd" in usage:
            return float(usage["est_usd"])
    return 0.0


def _make_retrieve_step(queries: list[str]) -> Any:
    async def _retrieve_hop() -> dict[str, Any]:
        hop_candidates: list[dict[str, Any]] = []
        async with get_session() as session:
            for q in queries:
                top_k, _all = await hybrid_search(
                    session,
                    query=q,
                    limit=settings.research_passages_per_hop,
                )
                hop_candidates.extend(top_k)
        return {"candidates": hop_candidates, "queries": queries}

    return _retrieve_hop


def _make_reflect_step(
    *,
    question: str,
    prompt: str,
    executed_queries: list[str],
    evidence_pool: list[dict[str, Any]],
    target_topics: list[str],
    iteration: int,
) -> Any:
    async def _reflect_hop() -> dict[str, Any]:
        if settings.anthropic_api_key:
            return await call_llm(
                system_prompt=coverage_critic_agent.system_prompt,
                user_input=prompt,
                model=coverage_critic_agent.model,
                max_tokens=coverage_critic_agent.max_tokens,
                output_schema=CoverageReflection,
            )
        fallback_ref = heuristic_reflect_coverage(
            question=question,
            executed_queries=executed_queries,
            evidence_pool=evidence_pool,
            target_topics=target_topics,
            iteration=iteration,
        )
        return {"output": fallback_ref.model_dump(mode="json")}

    return _reflect_hop


@client.create_function(
    fn_id="run-research-agent",
    name="Run Bounded Research Agent",
    trigger=inngest.TriggerEvent(event=RESEARCH_REQUESTED),
    concurrency=[
        inngest.Concurrency(limit=settings.concurrency_research_runs),
    ],
    retries=2,
)
async def run_research_agent(ctx: inngest.Context) -> dict[str, object]:
    start_clock = time.perf_counter()
    payload = ResearchRequested.model_validate(ctx.event.data)

    max_iterations = min(
        max(payload.max_iterations or settings.research_max_iterations, 1),
        5,
    )
    max_usd = float(
        payload.max_usd if payload.max_usd is not None else settings.research_max_usd
    )
    event_id = str(ctx.event.id) if ctx.event.id else None

    async def _init_row() -> dict[str, Any]:
        async with get_session() as session:
            await upsert_research_run(
                session,
                research_id=payload.research_id,
                event_id=event_id,
                question=payload.question,
                status="running",
                plan=[],
                hops=[],
                report_text=None,
                citations=[],
                retrieved=[],
                eval_summary={},
                confidence=None,
                iterations_used=0,
                total_cost_usd=0.0,
                latency_ms=0,
                completed_at=None,
            )
            await session.commit()
        return {"research_id": payload.research_id, "status": "running"}

    await ctx.step.run("record-research-start", _init_row)

    # Step 1: Decompose question into initial sub-queries
    async def _plan_subqueries() -> dict[str, Any]:
        if settings.anthropic_api_key:
            return await call_llm(
                system_prompt=query_planner_agent.system_prompt,
                user_input=f"Research Question: {payload.question}",
                model=query_planner_agent.model,
                max_tokens=query_planner_agent.max_tokens,
                output_schema=ResearchPlan,
            )
        fallback = heuristic_plan_queries(
            payload.question,
            max_subqueries=settings.research_max_subqueries_per_hop,
        )
        return {"output": fallback.model_dump(mode="json")}

    plan_step = await ctx.step.run("plan-subqueries", _plan_subqueries)
    total_cost_usd = _usage_usd(plan_step)
    plan_raw = plan_step.get("output", plan_step) if isinstance(plan_step, dict) else plan_step
    plan_obj = ResearchPlan.model_validate(plan_raw)

    initial_queries = plan_obj.subqueries[: settings.research_max_subqueries_per_hop]
    target_topics = list(plan_obj.target_topics)

    # Step 2..N: Bounded retrieval + reflection hops
    current_queries: list[str] = list(initial_queries)
    executed_queries: list[str] = []
    evidence_pool: list[dict[str, Any]] = []
    hops_trace: list[dict[str, object]] = []
    iterations_used = 0
    stop_reason = "completed"

    for iteration in range(1, max_iterations + 1):
        if total_cost_usd >= max_usd:
            stop_reason = "budget_exceeded"
            break

        if not current_queries:
            stop_reason = "no_followup_queries"
            break

        iterations_used = iteration
        hop_queries = [
            q for q in current_queries[: settings.research_max_subqueries_per_hop]
            if q not in executed_queries
        ]
        if not hop_queries:
            stop_reason = "queries_exhausted"
            break

        hop_retrieval = await ctx.step.run(
            f"hop-{iteration}-retrieve",
            _make_retrieve_step(list(hop_queries)),
        )
        raw_candidates = cast("list[dict[str, Any]]", hop_retrieval["candidates"])
        executed_queries.extend(hop_queries)

        evidence_pool, newly_added = merge_hop_candidates(evidence_pool, raw_candidates)

        # Convergence & budget checks before paying for a reflection LLM step
        if newly_added == 0 and iteration > 1:
            hops_trace.append(
                {
                    "hop": iteration,
                    "queries": hop_queries,
                    "newly_added_passages": 0,
                    "cumulative_passages": len(evidence_pool),
                    "decision": "stop_converged_zero_new_passages",
                    "rationale": "Hop produced zero new unique passages; halting loop.",
                    "step_cost_usd": 0.0,
                }
            )
            stop_reason = "converged"
            break

        if iteration >= max_iterations:
            hops_trace.append(
                {
                    "hop": iteration,
                    "queries": hop_queries,
                    "newly_added_passages": newly_added,
                    "cumulative_passages": len(evidence_pool),
                    "decision": "stop_max_iterations_reached",
                    "rationale": f"Reached max_iterations={max_iterations}.",
                    "step_cost_usd": 0.0,
                }
            )
            stop_reason = "max_iterations_reached"
            break

        if total_cost_usd >= max_usd:
            hops_trace.append(
                {
                    "hop": iteration,
                    "queries": hop_queries,
                    "newly_added_passages": newly_added,
                    "cumulative_passages": len(evidence_pool),
                    "decision": "stop_budget_exceeded",
                    "rationale": (
                        f"Accumulated cost ${total_cost_usd:.4f} "
                        f"reached max_usd=${max_usd:.4f}."
                    ),
                    "step_cost_usd": 0.0,
                }
            )
            stop_reason = "budget_exceeded"
            break

        # Run coverage critic step
        critic_prompt = (
            f"Research Question: {payload.question}\n"
            f"Target Topics: {', '.join(target_topics) if target_topics else 'N/A'}\n"
            f"Executed Queries So Far: {executed_queries}\n\n"
            f"Retrieved Evidence ({len(evidence_pool[:10])} top passages):\n"
            f"{format_evidence_context(evidence_pool[:10])}"
        )

        reflect_step = await ctx.step.run(
            f"hop-{iteration}-reflect",
            _make_reflect_step(
                question=payload.question,
                prompt=critic_prompt,
                executed_queries=list(executed_queries),
                evidence_pool=list(evidence_pool),
                target_topics=target_topics,
                iteration=iteration,
            ),
        )
        reflect_cost = _usage_usd(reflect_step)
        total_cost_usd += reflect_cost

        reflect_raw = (
            reflect_step.get("output", reflect_step)
            if isinstance(reflect_step, dict)
            else reflect_step
        )
        reflection = CoverageReflection.model_validate(reflect_raw)

        hops_trace.append(
            {
                "hop": iteration,
                "queries": hop_queries,
                "newly_added_passages": newly_added,
                "cumulative_passages": len(evidence_pool),
                "decision": "sufficient" if reflection.is_sufficient else "continue",
                "rationale": reflection.rationale,
                "missing_aspects": reflection.missing_aspects,
                "followup_queries": reflection.followup_queries,
                "step_cost_usd": round(reflect_cost, 6),
            }
        )

        if reflection.is_sufficient or not reflection.followup_queries:
            stop_reason = "sufficient_coverage"
            break

        current_queries = [
            q for q in reflection.followup_queries if q not in executed_queries
        ]

    # Step N+1: Synthesize the final cited research report
    prompt_limit = max(settings.retrieval_limit * 2, 8)
    top_evidence = evidence_pool[:prompt_limit]

    async def _synthesize_report() -> dict[str, Any]:
        if not top_evidence:
            return {
                "output": heuristic_synthesize_research(payload.question, []),
            }
        if settings.anthropic_api_key:
            synth_input = (
                f"Context Sources:\n{format_evidence_context(top_evidence)}\n\n"
                f"Research Question: {payload.question}\n\n"
                "Write a comprehensive, inline-cited technical research report:"
            )
            return await call_llm(
                system_prompt=research_synthesizer_agent.system_prompt,
                user_input=synth_input,
                model=research_synthesizer_agent.model,
                max_tokens=research_synthesizer_agent.max_tokens,
                output_schema=None,
            )
        return {
            "output": heuristic_synthesize_research(payload.question, top_evidence),
        }

    synth_step = await ctx.step.run("synthesize-report", _synthesize_report)
    total_cost_usd += _usage_usd(synth_step)
    report_text = str(
        synth_step.get("output", synth_step) if isinstance(synth_step, dict) else synth_step
    )
    citations = extract_cited_entries(report_text, top_evidence)

    # Step N+2: Evaluate the report with Week 5's deterministic checks & binary judges
    async def _evaluate_report() -> dict[str, Any]:
        det = run_deterministic_checks(
            answer_text=report_text,
            citations=citations,
            retrieved=evidence_pool,
            prompt_limit=prompt_limit,
        )
        failure_codes = list(det.failure_codes)

        judge_input = format_judge_input(
            question=payload.question,
            retrieved=evidence_pool,
            answer_text=report_text,
            prompt_limit=prompt_limit,
        )

        eval_cost = 0.0
        if settings.anthropic_api_key:
            f_res = await call_llm(
                system_prompt=faithfulness_judge_agent.system_prompt,
                user_input=judge_input,
                model=faithfulness_judge_agent.model,
                max_tokens=faithfulness_judge_agent.max_tokens,
                output_schema=BinaryVerdict,
            )
            eval_cost += _usage_usd(f_res)
            faith_verdict = BinaryVerdict.model_validate(f_res["output"])
        else:
            faith_verdict = heuristic_judge_faithfulness(
                question=payload.question,
                retrieved=evidence_pool,
                answer_text=report_text,
                prompt_limit=prompt_limit,
            )

        if faith_verdict.verdict == "FAIL" and faith_verdict.failure_code != "NONE":
            code_prefix = faith_verdict.failure_code.split(":")[0]
            if code_prefix not in failure_codes:
                failure_codes.append(code_prefix)

        if requires_disagreement_check(evidence_pool, prompt_limit=prompt_limit):
            if settings.anthropic_api_key:
                d_res = await call_llm(
                    system_prompt=disagreement_judge_agent.system_prompt,
                    user_input=judge_input,
                    model=disagreement_judge_agent.model,
                    max_tokens=disagreement_judge_agent.max_tokens,
                    output_schema=BinaryVerdict,
                )
                eval_cost += _usage_usd(d_res)
                dis_obj = BinaryVerdict.model_validate(d_res["output"])
            else:
                dis_obj = heuristic_judge_disagreement(
                    retrieved=evidence_pool,
                    answer_text=report_text,
                    prompt_limit=prompt_limit,
                )
            dis_verdict: str = dis_obj.verdict
            dis_critique = dis_obj.critique
            if dis_verdict == "FAIL" and "FM-5" not in failure_codes:
                failure_codes.append("FM-5")
        else:
            dis_verdict = "NOT_APPLICABLE"
            dis_critique = "Single author/publication in retrieved evidence pool."

        if not det.passed or faith_verdict.verdict == "FAIL":
            composite_score = 0.0
        elif dis_verdict == "FAIL":
            composite_score = 0.5
        else:
            composite_score = 1.0

        return {
            "deterministic_passed": det.passed,
            "checks": det.checks,
            "faithfulness_verdict": faith_verdict.verdict,
            "faithfulness_critique": faith_verdict.critique,
            "disagreement_verdict": dis_verdict,
            "disagreement_critique": dis_critique,
            "failure_codes": failure_codes,
            "composite_score": composite_score,
            "stop_reason": stop_reason,
            "eval_cost_usd": eval_cost,
        }

    eval_summary = await ctx.step.run("evaluate-report", _evaluate_report)
    total_cost_usd += float(eval_summary.get("eval_cost_usd", 0.0) or 0.0)
    confidence = float(eval_summary["composite_score"])
    failure_codes = cast("list[str]", eval_summary["failure_codes"])

    latency_ms = int((time.perf_counter() - start_clock) * 1000)
    final_status = "budget_exceeded" if stop_reason == "budget_exceeded" else "completed"

    # Step N+3: Persist completed research run
    async def _persist_final() -> dict[str, Any]:
        async with get_session() as session:
            await upsert_research_run(
                session,
                research_id=payload.research_id,
                event_id=event_id,
                question=payload.question,
                status=final_status,
                plan=initial_queries,
                hops=hops_trace,
                report_text=report_text,
                citations=citations,
                retrieved=evidence_pool,
                eval_summary=eval_summary,
                confidence=confidence,
                iterations_used=iterations_used,
                total_cost_usd=round(total_cost_usd, 6),
                latency_ms=latency_ms,
                completed_at=datetime.now(UTC),
            )
            await session.commit()
        return {
            "research_id": payload.research_id,
            "status": final_status,
            "iterations_used": iterations_used,
            "total_cost_usd": round(total_cost_usd, 6),
            "confidence": confidence,
        }

    persisted = await ctx.step.run("persist-research-run", _persist_final)

    completed_event = ResearchCompleted(
        research_id=payload.research_id,
        status=final_status,
        iterations_used=iterations_used,
        total_cost_usd=round(total_cost_usd, 6),
        confidence=confidence,
        failure_codes=failure_codes,
    )
    await ctx.step.send_event(
        "emit-research-completed",
        inngest.Event(
            name=RESEARCH_COMPLETED,
            data=completed_event.model_dump(mode="json"),
        ),
    )

    return persisted

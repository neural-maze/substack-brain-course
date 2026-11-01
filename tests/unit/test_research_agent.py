"""Unit tests for the Week 6 bounded research agent primitives (`bounded_loop`,
`merge_hop_candidates`, planner/critic/synthesizer roles, and deterministic checks).

Requires zero Docker containers, database connections, or external API keys.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from kb.agents.core import Agent, bounded_loop
from kb.agents.roles.researcher import (
    CoverageReflection,
    ResearchPlan,
    extract_cited_entries,
    heuristic_plan_queries,
    heuristic_reflect_coverage,
    heuristic_synthesize_digest,
    heuristic_synthesize_research,
    merge_hop_candidates,
)
from kb.evals.deterministic import run_deterministic_checks


class StepReply(BaseModel):
    step_num: int
    done: bool


async def test_bounded_loop_halts_when_should_stop_returns_true() -> None:
    executed_steps: list[str] = []

    class DummyStep:
        async def run(self, step_id: str, fn: Any) -> dict[str, Any]:
            executed_steps.append(step_id)
            idx = len(executed_steps)
            return {
                "output": {"step_num": idx, "done": idx == 2},
                "usage": {
                    "model": "claude-haiku-5-5",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "est_usd": 0.01,
                },
            }

    agent = Agent(
        name="loop-test",
        role="Test Loop Agent",
        system_prompt="Test",
        model="claude-haiku-5-5",
        output_schema=StepReply,
    )

    outputs = await bounded_loop(
        step=DummyStep(),
        agent=agent,
        initial_input="start",
        step_prefix="research-hop",
        should_stop=lambda out, _i: isinstance(out, StepReply) and out.done,
        next_input=lambda _out, i: f"input-{i + 1}",
        max_iterations=5,
        max_usd=0.50,
    )

    assert len(outputs) == 2
    assert executed_steps == ["research-hop-1", "research-hop-2"]


async def test_bounded_loop_halts_immediately_when_max_usd_exceeded() -> None:
    executed_steps: list[str] = []

    class ExpensiveStep:
        async def run(self, step_id: str, fn: Any) -> dict[str, Any]:
            executed_steps.append(step_id)
            idx = len(executed_steps)
            return {
                "output": {"step_num": idx, "done": False},
                "usage": {
                    "model": "claude-haiku-5-5",
                    "input_tokens": 10000,
                    "output_tokens": 5000,
                    "est_usd": 0.15,
                },
            }

    agent = Agent(
        name="cost-test",
        role="Cost Cap Agent",
        system_prompt="Test",
        model="claude-haiku-5-5",
        output_schema=StepReply,
    )

    outputs = await bounded_loop(
        step=ExpensiveStep(),
        agent=agent,
        initial_input="start",
        step_prefix="expensive-hop",
        should_stop=lambda _out, _i: False,
        next_input=lambda _out, i: f"input-{i + 1}",
        max_iterations=5,
        max_usd=0.25,
    )

    # 2 steps * $0.15 = $0.30 >= $0.25 -> halts after step 2 without running step 3
    assert len(outputs) == 2
    assert executed_steps == ["expensive-hop-1", "expensive-hop-2"]


def test_merge_hop_candidates_deduplicates_and_keeps_highest_rrf_score() -> None:
    hop_1 = [
        {
            "passage_id": "p-1",
            "text": "Inngest step.run memoizes step outputs.",
            "title": "Durable Execution",
            "author": "Miguel",
            "url": "https://theneuralmaze.com/p/durable",
            "published_at": "2026-10-01T00:00:00+00:00",
            "rrf_score": 0.032,
        },
        {
            "passage_id": "p-2",
            "text": "Reciprocal Rank Fusion combines rankings.",
            "title": "Hybrid Search",
            "author": "Paul",
            "url": "https://www.decodingai.com/p/rrf",
            "published_at": "2026-10-02T00:00:00+00:00",
            "rrf_score": 0.016,
        },
    ]
    hop_2 = [
        {
            "passage_id": "p-2",
            "text": "Reciprocal Rank Fusion combines rankings.",
            "title": "Hybrid Search",
            "author": "Paul",
            "url": "https://www.decodingai.com/p/rrf",
            "published_at": "2026-10-02T00:00:00+00:00",
            "rrf_score": 0.048,
            "claim_id": "c-222",
            "claim_text": "RRF requires no score calibration.",
            "claim_type": "opinion",
        },
        {
            "passage_id": "p-3",
            "text": "Small language models reduce latency by 40%.",
            "title": "SLMs in Production",
            "author": "Sebastian",
            "url": "https://magazine.sebastianraschka.com/p/slm",
            "published_at": "2026-10-03T00:00:00+00:00",
            "rrf_score": 0.025,
        },
    ]

    merged, newly_added = merge_hop_candidates(hop_1, hop_2)
    assert newly_added == 1
    assert [m["passage_id"] for m in merged] == ["p-2", "p-1", "p-3"]
    assert merged[0]["rrf_score"] == 0.048
    assert merged[0]["claim_id"] == "c-222"
    assert merged[0]["claim_type"] == "opinion"


def test_heuristic_planner_and_critic_produce_valid_schemas() -> None:
    plan = heuristic_plan_queries(
        "How does Reciprocal Rank Fusion compare to pgvector dense search and Memgraph?",
        max_subqueries=3,
    )
    assert isinstance(plan, ResearchPlan)
    assert 1 <= len(plan.subqueries) <= 3
    assert "Reciprocal Rank Fusion" in plan.target_topics

    # Reflection when topic is still missing on hop 1
    evidence = [
        {
            "passage_id": "p-1",
            "title": "Vector Search",
            "text": "pgvector indexes cosine similarity.",
            "author": "Miguel",
            "url": "https://theneuralmaze.com/p/vec",
            "rrf_score": 0.03,
        }
    ]
    ref_hop1 = heuristic_reflect_coverage(
        question="How does Reciprocal Rank Fusion compare to pgvector?",
        executed_queries=plan.subqueries,
        evidence_pool=evidence,
        target_topics=["Reciprocal Rank Fusion", "pgvector"],
        iteration=1,
    )
    assert isinstance(ref_hop1, CoverageReflection)
    assert ref_hop1.is_sufficient is False
    assert "Reciprocal Rank Fusion" in ref_hop1.missing_aspects
    assert len(ref_hop1.followup_queries) >= 1


def test_heuristic_research_and_digest_pass_level1_deterministic_checks() -> None:
    passages = [
        {
            "passage_id": "p-101",
            "claim_id": "c-101",
            "claim_text": "Three-way RRF lifts Recall@5 to 0.94 across publications.",
            "claim_type": "measurement",
            "text": "Three-way RRF lifts Recall@5 to 0.94 across publications.",
            "title": "Graph Fusion",
            "author": "Miguel Otero Pedrido",
            "url": "https://theneuralmaze.com/p/graph-fusion",
            "published_at": "2026-10-20T00:00:00+00:00",
            "valid_to": None,
        },
        {
            "passage_id": "p-102",
            "claim_id": "c-102",
            "claim_text": "Hybrid search with reranking should be carefully benchmarked.",
            "claim_type": "opinion",
            "text": "Hybrid search with reranking should be carefully benchmarked.",
            "title": "Pragmatic RAG",
            "author": "Paul Iusztin",
            "url": "https://www.decodingai.com/p/pragmatic-rag",
            "published_at": "2026-10-21T00:00:00+00:00",
            "valid_to": None,
        },
    ]

    report = heuristic_synthesize_research(
        "How do authors evaluate hybrid RAG retrieval?",
        passages,
    )
    citations = extract_cited_entries(report, passages)
    det_res = run_deterministic_checks(
        answer_text=report,
        citations=citations,
        retrieved=passages,
        prompt_limit=5,
    )
    assert det_res.passed is True
    assert det_res.failure_codes == []

    digest_md = heuristic_synthesize_digest(
        "Weekly Digest (2026-10-22 to 2026-10-29)",
        passages,
        [{"slug": "rag", "name": "RAG", "is_cross_publication": True, "claim_count": 2}],
    )
    digest_cits = extract_cited_entries(digest_md, passages)
    det_digest = run_deterministic_checks(
        answer_text=digest_md,
        citations=digest_cits,
        retrieved=passages,
        prompt_limit=10,
    )
    assert det_digest.passed is True
    assert det_digest.failure_codes == []


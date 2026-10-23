"""Calibrate the Week 5 binary LLM judges against human-annotated gold traces
(`evals/labeled_traces.jsonl`), computing Hamel Husain's TPR (True Positive Rate
on failures) and TNR (True Negative Rate on passes).

Usage:
    uv run python scripts/calibrate_judges.py --mode offline
    uv run python scripts/calibrate_judges.py --mode live
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from kb.agents.providers import call_llm
from kb.config import settings
from kb.evals.deterministic import run_deterministic_checks
from kb.evals.judges import (
    BinaryVerdict,
    disagreement_judge_agent,
    faithfulness_judge_agent,
    format_judge_input,
    requires_disagreement_check,
)
from kb.evals.metrics import (
    CalibrationReport,
    compute_tpr_tnr,
    evaluate_trace_offline,
    load_labeled_traces,
)


async def _evaluate_trace_live(trace: dict[str, Any]) -> dict[str, Any]:
    question = str(trace["question"])
    retrieved = list(trace.get("retrieved") or [])
    citations = list(trace.get("citations") or [])
    answer_text = str(trace["answer_text"])

    det = run_deterministic_checks(
        answer_text=answer_text,
        citations=citations,
        retrieved=retrieved,
    )
    if not det.passed:
        return {
            "verdict": "FAIL",
            "failure_codes": det.failure_codes,
            "critique": f"Failed deterministic checks: {det.failure_codes}",
        }

    prompt = format_judge_input(
        question=question,
        retrieved=retrieved,
        answer_text=answer_text,
    )
    faith_resp = await call_llm(
        system_prompt=faithfulness_judge_agent.system_prompt,
        user_input=prompt,
        model=faithfulness_judge_agent.model,
        max_tokens=faithfulness_judge_agent.max_tokens,
        output_schema=BinaryVerdict,
    )
    faith_obj = BinaryVerdict.model_validate(faith_resp["output"])
    if faith_obj.verdict == "FAIL":
        return {
            "verdict": "FAIL",
            "failure_codes": [faith_obj.failure_code],
            "critique": faith_obj.critique,
        }

    if requires_disagreement_check(retrieved):
        dis_resp = await call_llm(
            system_prompt=disagreement_judge_agent.system_prompt,
            user_input=prompt,
            model=disagreement_judge_agent.model,
            max_tokens=disagreement_judge_agent.max_tokens,
            output_schema=BinaryVerdict,
        )
        dis_obj = BinaryVerdict.model_validate(dis_resp["output"])
        if dis_obj.verdict == "FAIL":
            return {
                "verdict": "FAIL",
                "failure_codes": [dis_obj.failure_code],
                "critique": dis_obj.critique,
            }

    return {
        "verdict": "PASS",
        "failure_codes": [],
        "critique": faith_obj.critique,
    }


async def run_calibration(mode: str) -> CalibrationReport:
    traces = load_labeled_traces()
    predictions: list[tuple[str, str, dict[str, Any]]] = []

    for trace in traces:
        human_verdict = str(trace["human_verdict"]).upper()
        if mode == "live" and settings.anthropic_api_key:
            eval_res = await _evaluate_trace_live(trace)
            critique = str(eval_res["critique"])
        else:
            eval_res = evaluate_trace_offline(trace)
            critique = str(eval_res["faithfulness_critique"])

        pred_verdict = str(eval_res["verdict"]).upper()
        predictions.append(
            (
                human_verdict,
                pred_verdict,
                {
                    "trace_id": trace["trace_id"],
                    "human_failure_code": trace.get("failure_code", "NONE"),
                    "predicted_failure_codes": eval_res["failure_codes"],
                    "open_code_note": trace.get("open_code_note", ""),
                    "judge_critique": critique,
                },
            )
        )

    return compute_tpr_tnr(
        predictions,
        min_tpr=settings.eval_min_tpr,
        min_tnr=settings.eval_min_tnr,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Calibrate Week 5 binary LLM judges with TPR and TNR."
    )
    parser.add_argument(
        "--mode",
        choices=("offline", "live"),
        default="offline",
        help="'offline' runs deterministic + heuristic judges; 'live' calls Anthropic.",
    )
    args = parser.parse_args()

    report = asyncio.run(run_calibration(args.mode))
    print(json.dumps(report.to_dict(), indent=2))
    if not report.aligned:
        sys.exit(1)


if __name__ == "__main__":
    main()


"""Week 5 unit tests: Inngest evaluation functions (`evaluate-answer`,
`run-eval-suite`), failure taxonomy fixture, MCP tool registration (`kb_eval_report`),
and event schemas (`EvalRunRequested`, `AnswerEvaluated`).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from kb.config import settings
from kb.functions import FUNCTIONS
from kb.functions.evals import evaluate_answer, run_eval_suite
from kb.mcp.server import mcp
from kb.schemas.events import (
    ANSWER_EVALUATED,
    ANSWER_PRODUCED,
    EVAL_RUN_REQUESTED,
    AnswerEvaluated,
    EvalRunRequested,
)


def test_week5_eval_functions_are_registered_in_functions_list() -> None:
    fn_ids = {fn.get_config("http://localhost:8000").main.id for fn in FUNCTIONS}
    assert "substack-brain-evaluate-answer" in fn_ids
    assert "substack-brain-run-eval-suite" in fn_ids


def test_evaluate_answer_triggers_on_answer_produced() -> None:
    config = evaluate_answer.get_config("http://localhost:8000").main
    event_names = {getattr(t, "event", None) for t in config.triggers}
    assert event_names == {ANSWER_PRODUCED}


def test_run_eval_suite_triggers_on_eval_run_requested() -> None:
    config = run_eval_suite.get_config("http://localhost:8000").main
    event_names = {getattr(t, "event", None) for t in config.triggers}
    assert event_names == {EVAL_RUN_REQUESTED}


def test_taxonomy_yaml_defines_all_six_axial_failure_modes() -> None:
    data = yaml.safe_load(Path(settings.eval_taxonomy_file).read_text(encoding="utf-8"))
    modes = data["failure_modes"]
    codes = [m["code"] for m in modes]
    assert codes == ["FM-1", "FM-2", "FM-3", "FM-4", "FM-5", "FM-6"]


def test_mcp_server_registers_kb_eval_report_as_tenth_tool() -> None:
    tools_dict = mcp._tool_manager._tools
    assert "kb_eval_report" in tools_dict
    assert len(tools_dict) == 10


def test_week5_event_schemas() -> None:
    req = EvalRunRequested(suite_name="golden_v1", include_failures_table=True)
    assert req.model_dump(mode="json") == {
        "suite_name": "golden_v1",
        "include_failures_table": True,
    }

    evaluated = AnswerEvaluated(
        snapshot_id="snap-123",
        composite_score=1.0,
        deterministic_passed=True,
        faithfulness_verdict="PASS",
        disagreement_verdict="NOT_APPLICABLE",
        failure_codes=[],
    )
    dumped = evaluated.model_dump(mode="json")
    assert dumped["snapshot_id"] == "snap-123"
    assert dumped["composite_score"] == 1.0
    assert ANSWER_EVALUATED == "kb/answer.evaluated"


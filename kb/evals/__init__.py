"""Evaluation suite package (Week 5): deterministic checks, binary LLM judges,
and TPR/TNR calibration + retrieval variant metrics.
"""

from kb.evals.deterministic import DeterministicEvalResult, run_deterministic_checks
from kb.evals.judges import (
    BinaryVerdict,
    disagreement_judge_agent,
    faithfulness_judge_agent,
    format_judge_input,
    heuristic_judge_disagreement,
    heuristic_judge_faithfulness,
    requires_disagreement_check,
)
from kb.evals.metrics import (
    CalibrationReport,
    compute_mrr,
    compute_recall_at_k,
    compute_tpr_tnr,
    evaluate_trace_offline,
    load_labeled_traces,
    rank_fixture_corpus_by_variant,
    run_benchmark_variants,
)

__all__ = [
    "BinaryVerdict",
    "CalibrationReport",
    "DeterministicEvalResult",
    "compute_mrr",
    "compute_recall_at_k",
    "compute_tpr_tnr",
    "disagreement_judge_agent",
    "evaluate_trace_offline",
    "faithfulness_judge_agent",
    "format_judge_input",
    "heuristic_judge_disagreement",
    "heuristic_judge_faithfulness",
    "load_labeled_traces",
    "rank_fixture_corpus_by_variant",
    "requires_disagreement_check",
    "run_benchmark_variants",
    "run_deterministic_checks",
]


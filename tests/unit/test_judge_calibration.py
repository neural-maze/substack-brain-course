"""Unit tests for Week 5 judge calibration (TPR & TNR) and retrieval variant metrics.

Verifies Hamel Husain's binary judge alignment criteria and the self-contained
`evals/` fixtures without external network or database dependencies.
"""

from __future__ import annotations

from kb.evals.judges import BinaryVerdict
from kb.evals.metrics import (
    compute_mrr,
    compute_recall_at_k,
    compute_tpr_tnr,
    evaluate_trace_offline,
    load_labeled_traces,
    run_benchmark_variants,
)


def test_binary_verdict_schema_requires_critique_and_binary_verdict() -> None:
    verdict = BinaryVerdict(
        critique="Verified that [1] directly supports the claim and matches claim_type.",
        verdict="PASS",
        failure_code="NONE",
    )
    assert verdict.verdict == "PASS"
    assert verdict.failure_code == "NONE"
    assert len(verdict.critique) >= 10


def test_compute_tpr_tnr_detects_rubber_stamp_and_aligned_judges() -> None:
    # A rubber-stamp judge that marks everything PASS has TNR=1.0 but TPR=0.0
    rubber_stamp = [
        ("FAIL", "PASS", {"trace_id": "t1"}),
        ("FAIL", "PASS", {"trace_id": "t2"}),
        ("PASS", "PASS", {"trace_id": "t3"}),
        ("PASS", "PASS", {"trace_id": "t4"}),
    ]
    bad_report = compute_tpr_tnr(rubber_stamp, min_tpr=0.90, min_tnr=0.90)
    assert bad_report.tpr == 0.0
    assert bad_report.tnr == 1.0
    assert bad_report.aligned is False
    assert len(bad_report.disagreements) == 2

    # A well-calibrated judge with 100% TPR and 100% TNR passes alignment
    calibrated = [
        ("FAIL", "FAIL", {"trace_id": "t1"}),
        ("FAIL", "FAIL", {"trace_id": "t2"}),
        ("PASS", "PASS", {"trace_id": "t3"}),
        ("PASS", "PASS", {"trace_id": "t4"}),
    ]
    good_report = compute_tpr_tnr(calibrated, min_tpr=0.90, min_tnr=0.90)
    assert good_report.tpr == 1.0
    assert good_report.tnr == 1.0
    assert good_report.aligned is True
    assert good_report.disagreements == []


def test_labeled_traces_fixture_calibrates_above_90_percent_tpr_and_tnr() -> None:
    traces = load_labeled_traces()
    assert len(traces) == 20

    predictions = []
    for trace in traces:
        res = evaluate_trace_offline(trace)
        predictions.append(
            (
                str(trace["human_verdict"]),
                str(res["verdict"]),
                {"trace_id": trace["trace_id"]},
            )
        )

    report = compute_tpr_tnr(predictions, min_tpr=0.90, min_tnr=0.90)
    assert report.human_passes == 10
    assert report.human_fails == 10
    assert report.tpr >= 0.90
    assert report.tnr >= 0.90
    assert report.aligned is True


def test_recall_at_k_and_mrr_calculations() -> None:
    retrieved = ["p-3", "p-1", "p-5", "p-2", "p-9"]
    expected = ["p-1", "p-2"]
    assert compute_recall_at_k(retrieved, expected, k=5) == 1.0
    assert compute_recall_at_k(retrieved, expected, k=2) == 0.5
    # First relevant hit ("p-1") is at rank 2 -> MRR = 1/2 = 0.5
    assert compute_mrr(retrieved, expected, k=5) == 0.5


def test_golden_benchmark_variant_experiment_proves_3way_rrf_lift() -> None:
    result = run_benchmark_variants()
    summary = result["variants_summary"]
    assert set(summary.keys()) == {
        "sparse_bm25",
        "dense_pgvector",
        "hybrid_2way_rrf_k60",
        "hybrid_3way_rrf_k60",
    }

    sparse_rec = summary["sparse_bm25"]["recall_at_5"]
    dense_rec = summary["dense_pgvector"]["recall_at_5"]
    rrf2_rec = summary["hybrid_2way_rrf_k60"]["recall_at_5"]
    rrf3_rec = summary["hybrid_3way_rrf_k60"]["recall_at_5"]

    # 3-way RRF beats or matches 2-way RRF, and both beat single-engine retrieval
    assert rrf3_rec >= rrf2_rec
    assert rrf2_rec > sparse_rec
    assert rrf2_rec > dense_rec
    assert summary["hybrid_3way_rrf_k60"]["cross_pub_coverage"] == 1.0


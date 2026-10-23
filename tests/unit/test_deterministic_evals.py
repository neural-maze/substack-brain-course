"""Unit tests for Week 5 Level 1 deterministic code evaluators (`kb/evals/deterministic.py`).

Runs in milliseconds with zero network, database, or LLM dependencies.
"""

from __future__ import annotations

from kb.evals.deterministic import (
    extract_inline_citation_indices,
    run_deterministic_checks,
)


def test_extract_inline_citation_indices_preserves_order_and_deduplicates() -> None:
    text = "Inngest memoizes steps [1] while RRF combines rankings [2] and [1]."
    assert extract_inline_citation_indices(text) == [1, 2]


def test_deterministic_checks_pass_on_valid_grounded_snapshot() -> None:
    retrieved = [
        {"passage_id": "p-1", "text": "Passage one text.", "valid_to": None},
        {"passage_id": "p-2", "text": "Passage two text.", "valid_to": None},
    ]
    citations = [
        {"citation_idx": 1, "passage_id": "p-1", "valid_to": None},
    ]
    res = run_deterministic_checks(
        answer_text="Grounded claim citing passage one [1].",
        citations=citations,
        retrieved=retrieved,
    )
    assert res.passed is True
    assert res.failure_codes == []
    assert res.score == 1.0


def test_deterministic_checks_catch_out_of_bounds_phantom_citation_fm1() -> None:
    retrieved = [
        {"passage_id": "p-1", "text": "Passage one text.", "valid_to": None},
    ]
    citations = [
        {"citation_idx": 1, "passage_id": "p-1", "valid_to": None},
    ]
    res = run_deterministic_checks(
        answer_text="Grounded [1] plus phantom [4].",
        citations=citations,
        retrieved=retrieved,
    )
    assert res.passed is False
    assert res.checks["citation_bounds_valid"] is False
    assert "FM-1" in res.failure_codes


def test_deterministic_checks_catch_unretrieved_citation_passage_id_fm1() -> None:
    retrieved = [
        {"passage_id": "p-1", "text": "Passage one text.", "valid_to": None},
    ]
    citations = [
        {"citation_idx": 1, "passage_id": "p-nonexistent", "valid_to": None},
    ]
    res = run_deterministic_checks(
        answer_text="Answer with dangling passage_id [1].",
        citations=citations,
        retrieved=retrieved,
    )
    assert res.passed is False
    assert res.checks["citations_subset_of_retrieved"] is False
    assert "FM-1" in res.failure_codes


def test_deterministic_checks_catch_superseded_source_fm2() -> None:
    retrieved = [
        {"passage_id": "p-old", "text": "Superseded text.", "valid_to": "2026-10-01T00:00:00Z"},
    ]
    citations = [
        {"citation_idx": 1, "passage_id": "p-old"},
    ]
    res = run_deterministic_checks(
        answer_text="Answer citing a stale revision [1].",
        citations=citations,
        retrieved=retrieved,
    )
    assert res.passed is False
    assert res.checks["no_superseded_sources_cited"] is False
    assert "FM-2" in res.failure_codes


def test_deterministic_checks_catch_speculation_on_empty_retrieval_fm3() -> None:
    res = run_deterministic_checks(
        answer_text="When configuring Cilium BGP, enable bgpControlPlane in Helm.",
        citations=[],
        retrieved=[],
    )
    assert res.passed is False
    assert res.checks["abstain_or_cite_discipline"] is False
    assert "FM-3" in res.failure_codes


def test_deterministic_checks_pass_on_clean_abstain_when_empty() -> None:
    res = run_deterministic_checks(
        answer_text="No relevant passages were found in the knowledge base for this question.",
        citations=[],
        retrieved=[],
    )
    assert res.passed is True
    assert res.failure_codes == []


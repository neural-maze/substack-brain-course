"""Level 1 deterministic code evaluators (Week 5).

Following Hamel Husain's evaluation hierarchy, structural, referential, and
temporal validity checks are executed in deterministic Python first—at 0 USD
cost and <1ms latency—before any LLM-as-a-judge is invoked.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

INLINE_CITATION_RE = re.compile(r"\[(\d+)\]")
ABSTAIN_PHRASES = (
    "no relevant passages were found",
    "coverage of this topic is limited",
    "set anthropic_api_key to get an answer",
)


@dataclass(frozen=True)
class DeterministicEvalResult:
    passed: bool
    checks: dict[str, bool]
    failure_codes: list[str]
    score: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def extract_inline_citation_indices(answer_text: str) -> list[int]:
    """Return all unique integer indices `[k]` appearing in `answer_text` in order."""
    seen: set[int] = set()
    indices: list[int] = []
    for match in INLINE_CITATION_RE.finditer(answer_text):
        idx = int(match.group(1))
        if idx not in seen:
            seen.add(idx)
            indices.append(idx)
    return indices


def is_abstain_answer(answer_text: str) -> bool:
    lower = answer_text.lower()
    return any(phrase in lower for phrase in ABSTAIN_PHRASES)


def run_deterministic_checks(
    *,
    answer_text: str,
    citations: list[dict[str, Any]],
    retrieved: list[dict[str, Any]],
    prompt_limit: int = 5,
) -> DeterministicEvalResult:
    """Run all four deterministic assertions on a snapshot (`FM-1`, `FM-2`, `FM-3`):

    1. `citation_bounds_valid` (FM-1): Every inline `[k]` is within
       `1..len(retrieved[:prompt_limit])` and has a matching entry in `citations`.
    2. `citations_subset_of_retrieved` (FM-1): Every `passage_id` in `citations`
       exists in `retrieved`.
    3. `no_superseded_sources_cited` (FM-2): No cited passage or claim has `valid_to` set.
    4. `abstain_or_cite_discipline` (FM-3):
       - If `retrieved` is empty, `answer_text` must abstain and have zero citations.
       - If `retrieved` is non-empty and `answer_text` is not an abstain message, it must
         include at least one inline citation `[k]`.
    """
    inline_indices = extract_inline_citation_indices(answer_text)
    max_valid_idx = min(len(retrieved), prompt_limit)
    citation_idx_map = {
        int(c["citation_idx"]): c
        for c in citations
        if isinstance(c, dict) and "citation_idx" in c
    }

    # Check 1: citation_bounds_valid (FM-1)
    bounds_ok = True
    if not retrieved and inline_indices:
        bounds_ok = False
    for idx in inline_indices:
        if idx < 1 or idx > max_valid_idx or idx not in citation_idx_map:
            bounds_ok = False
            break

    # Check 2: citations_subset_of_retrieved (FM-1)
    retrieved_by_pid = {
        str(r["passage_id"]): r
        for r in retrieved
        if isinstance(r, dict) and "passage_id" in r
    }
    subset_ok = all(
        str(c.get("passage_id", "")) in retrieved_by_pid for c in citations
    )

    # Check 3: no_superseded_sources_cited (FM-2)
    freshness_ok = True
    for c in citations:
        if c.get("valid_to") is not None:
            freshness_ok = False
            break
        pid = str(c.get("passage_id", ""))
        ret_row = retrieved_by_pid.get(pid)
        if ret_row is not None and ret_row.get("valid_to") is not None:
            freshness_ok = False
            break

    # Check 4: abstain_or_cite_discipline (FM-3)
    abstain = is_abstain_answer(answer_text)
    if not retrieved:
        discipline_ok = abstain and len(inline_indices) == 0 and len(citations) == 0
    else:
        discipline_ok = True if abstain else len(inline_indices) >= 1 and len(citations) >= 1

    checks = {
        "citation_bounds_valid": bounds_ok,
        "citations_subset_of_retrieved": subset_ok,
        "no_superseded_sources_cited": freshness_ok,
        "abstain_or_cite_discipline": discipline_ok,
    }

    failure_codes: list[str] = []
    if not bounds_ok or not subset_ok:
        failure_codes.append("FM-1")
    if not freshness_ok:
        failure_codes.append("FM-2")
    if not discipline_ok:
        failure_codes.append("FM-3")

    passed_count = sum(1 for v in checks.values() if v)
    score = round(passed_count / len(checks), 4)
    return DeterministicEvalResult(
        passed=all(checks.values()),
        checks=checks,
        failure_codes=failure_codes,
        score=score,
    )

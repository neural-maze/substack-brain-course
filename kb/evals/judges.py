"""Level 2 narrow binary LLM judges (`PASS` / `FAIL` + mandatory `critique`).

Follows Hamel Husain's LLM-as-a-Judge methodology:
1. No 1–5 Likert scales. Every judge is a strict binary classifier (`PASS` or `FAIL`).
2. `critique` comes first in the schema so the model reasons over the trace before
   emitting `verdict`.
3. Separate judges per failure mode (`faithfulness_judge_agent` for FM-3/FM-4,
   `disagreement_judge_agent` for FM-5) so evaluation criteria never bleed together.
4. Deterministic heuristic fallback when `ANTHROPIC_API_KEY` is unset, allowing
   offline calibration and CI tests to run without external API keys.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from kb.agents.core import Agent
from kb.agents.prompts import (
    DISAGREEMENT_JUDGE_SYSTEM_PROMPT,
    FAITHFULNESS_JUDGE_SYSTEM_PROMPT,
)
from kb.config import settings
from kb.evals.deterministic import extract_inline_citation_indices, is_abstain_answer

VerdictLabel = Literal["PASS", "FAIL"]
JudgeFailureCode = Literal[
    "NONE",
    "FM-3",
    "FM-4",
    "FM-5",
]


class BinaryVerdict(BaseModel):
    """Hamel Husain binary judge output: mandatory critique first, binary verdict second."""

    critique: str = Field(
        min_length=10,
        description=(
            "Step-by-step verification comparing each sentence and citation in the "
            "answer against the retrieved passages and claim types."
        ),
    )
    verdict: VerdictLabel = Field(
        description="Strict binary outcome: PASS only if zero violations exist, otherwise FAIL."
    )
    failure_code: JudgeFailureCode = "NONE"


faithfulness_judge_agent: Agent[BinaryVerdict] = Agent(
    name="faithfulness-judge",
    role="Binary Faithfulness & Epistemic Attribution Judge",
    system_prompt=FAITHFULNESS_JUDGE_SYSTEM_PROMPT,
    model=settings.eval_judge_model,
    output_schema=BinaryVerdict,
    max_tokens=settings.eval_judge_max_tokens,
)

disagreement_judge_agent: Agent[BinaryVerdict] = Agent(
    name="disagreement-judge",
    role="Binary Cross-Publication Disagreement Judge",
    system_prompt=DISAGREEMENT_JUDGE_SYSTEM_PROMPT,
    model=settings.eval_judge_model,
    output_schema=BinaryVerdict,
    max_tokens=settings.eval_judge_max_tokens,
)


def requires_disagreement_check(retrieved: list[dict[str, Any]], prompt_limit: int = 5) -> bool:
    """Return True if the top retrieved candidates span >= 2 distinct authors or publications."""
    top = retrieved[:prompt_limit]
    authors = {
        str(r.get("author")).strip().lower()
        for r in top
        if r.get("author") and str(r.get("author")).strip().lower() != "unknown"
    }
    pubs = {
        str(r.get("publication")).strip().lower()
        for r in top
        if r.get("publication")
    }
    return len(authors) >= 2 or len(pubs) >= 2


def format_judge_input(
    *,
    question: str,
    retrieved: list[dict[str, Any]],
    answer_text: str,
    prompt_limit: int = 5,
) -> str:
    """Format the trace for `faithfulness_judge_agent` or `disagreement_judge_agent`."""
    blocks: list[str] = []
    for idx, p in enumerate(retrieved[:prompt_limit], start=1):
        author = p.get("author") or "Unknown"
        pub = p.get("publication") or p.get("url") or "Unknown"
        claim_type = p.get("claim_type") or "none"
        claim_text = p.get("claim_text") or ""
        blocks.append(
            f"[{idx}] Author: {author} | Source: {pub} | Epistemic Type: {claim_type}\n"
            f"Claim: {claim_text}\n"
            f"Passage Text: {p.get('text', '')}"
        )
    context_str = "\n\n".join(blocks) if blocks else "(0 passages retrieved)"
    return (
        f"Question: {question}\n\n"
        f"Retrieved Context:\n{context_str}\n\n"
        f"Synthesized Answer:\n{answer_text}"
    )


_STOPWORDS = frozenset(
    {
        "the", "and", "for", "that", "with", "this", "from", "are", "was", "were",
        "have", "has", "had", "not", "but", "what", "when", "where", "which", "who",
        "how", "why", "can", "will", "into", "over", "under", "more", "than", "only",
        "also", "their", "they", "them", "these", "those", "such", "some", "other",
        "while", "whereas", "across", "using", "uses", "used", "does",
    }
)


def _content_tokens(text: str) -> set[str]:
    return {
        t
        for t in re.findall(r"[a-z0-9_]+", text.lower())
        if len(t) >= 4 and t not in _STOPWORDS
    }


def heuristic_judge_faithfulness(
    *,
    question: str,
    retrieved: list[dict[str, Any]],
    answer_text: str,
    prompt_limit: int = 5,
) -> BinaryVerdict:
    """Deterministic fallback judge for FM-3 and FM-4 when `ANTHROPIC_API_KEY` is not set."""
    top = retrieved[:prompt_limit]
    if not top:
        if is_abstain_answer(answer_text):
            return BinaryVerdict(
                critique=(
                    "Retrieved context is empty and the synthesizer properly abstained "
                    "without fabricating unsupported claims."
                ),
                verdict="PASS",
                failure_code="NONE",
            )
        return BinaryVerdict(
            critique=(
                "Retrieved context is empty, yet the answer speculated from pre-training "
                "memory instead of abstaining."
            ),
            verdict="FAIL",
            failure_code="FM-3",
        )

    if is_abstain_answer(answer_text):
        return BinaryVerdict(
            critique="Answer abstained cleanly when context coverage was limited.",
            verdict="PASS",
            failure_code="NONE",
        )

    # Check epistemic inflation: if all cited passages are 'prediction' or 'opinion',
    # flag answers claiming "empirical benchmarks prove as an established measurement"
    lower_answer = answer_text.lower()
    non_measurements = [
        p for p in top if p.get("claim_type") in ("prediction", "opinion")
    ]
    if (
        len(non_measurements) == len(top)
        and re.search(
            r"\b(benchmarks?\s+prove|established\s+measurement|empirically\s+proven)\b",
            lower_answer,
        )
    ):
        return BinaryVerdict(
            critique=(
                "Epistemic inflation detected: the retrieved source is tagged as an "
                f"'{top[0].get('claim_type')}', but the answer frames it as an "
                "empirically proven benchmark measurement."
            ),
            verdict="FAIL",
            failure_code="FM-4",
        )

    # Check token grounding overlap between answer and retrieved passages
    context_blob = " ".join(
        f"{p.get('text', '')} {p.get('claim_text', '')} {p.get('author', '')}"
        for p in top
    )
    ctx_tokens = _content_tokens(context_blob) | _content_tokens(question)
    ans_tokens = _content_tokens(answer_text)

    if not ans_tokens:
        return BinaryVerdict(
            critique="Answer contains no substantive technical assertions to verify.",
            verdict="FAIL",
            failure_code="FM-4",
        )

    overlap_ratio = len(ans_tokens & ctx_tokens) / max(len(ans_tokens), 1)
    if overlap_ratio < 0.42:
        unmatched = sorted(ans_tokens - ctx_tokens)[:6]
        return BinaryVerdict(
            critique=(
                "Answer introduces unsupported claims or terminology not grounded in "
                f"the retrieved passages (unmatched tokens: {', '.join(unmatched)})."
            ),
            verdict="FAIL",
            failure_code="FM-4",
        )

    return BinaryVerdict(
        critique=(
            f"All cited assertions align with the retrieved passages (grounding overlap "
            f"{overlap_ratio:.0%}) and preserve the source's epistemic claim_type."
        ),
        verdict="PASS",
        failure_code="NONE",
    )


def heuristic_judge_disagreement(
    *,
    retrieved: list[dict[str, Any]],
    answer_text: str,
    prompt_limit: int = 5,
) -> BinaryVerdict:
    """Deterministic fallback judge for FM-5 (Cross-Publication Disagreement Coverage)."""
    top = retrieved[:prompt_limit]
    if not requires_disagreement_check(top, prompt_limit=prompt_limit):
        return BinaryVerdict(
            critique="Single author or publication in context; no cross-publication conflict.",
            verdict="PASS",
            failure_code="NONE",
        )

    cited_indices = set(extract_inline_citation_indices(answer_text))
    cited_authors = {
        str(top[idx - 1].get("author")).strip().lower()
        for idx in cited_indices
        if 1 <= idx <= len(top) and top[idx - 1].get("author")
    }
    cited_pubs = {
        str(top[idx - 1].get("publication")).strip().lower()
        for idx in cited_indices
        if 1 <= idx <= len(top) and top[idx - 1].get("publication")
    }

    if len(cited_authors) < 2 and len(cited_pubs) < 2 and len(cited_indices) < 2:
        return BinaryVerdict(
            critique=(
                "Retrieved context contains perspectives from multiple authors/publications, "
                "but the synthesized answer only cited one source and suppressed the "
                "competing perspective."
            ),
            verdict="FAIL",
            failure_code="FM-5",
        )

    return BinaryVerdict(
        critique=(
            "Synthesized answer explicitly surfaces and cites multiple authors/publications "
            "from the retrieved context."
        ),
        verdict="PASS",
        failure_code="NONE",
    )

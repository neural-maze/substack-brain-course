"""Thin async wrappers around Anthropic (and optional OpenAI fallback) with token
and USD cost calculation.

Every call returns a `"usage"` dictionary shaped for `CostCaptureMiddleware`
(`kb/inngest_client.py`) and can also write directly to the `costs` table when
invoked outside an Inngest step middleware context.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import anthropic
from pydantic import BaseModel

from kb.config import settings
from kb.db.queries import insert_cost
from kb.db.session import get_session

_anthropic_client: anthropic.AsyncAnthropic | None = None

# Per-million-token pricing table (input_usd, output_usd)
MODEL_PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-20250514": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-sonnet-5-5": (3.00, 15.00),
    "claude-3-5-sonnet-latest": (3.00, 15.00),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
}


def get_anthropic_client() -> anthropic.AsyncAnthropic | None:
    """Return a lazily-initialized `AsyncAnthropic` client if `ANTHROPIC_API_KEY` is set."""
    global _anthropic_client
    if not settings.anthropic_api_key:
        return None
    if _anthropic_client is None:
        _anthropic_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _anthropic_client


def estimate_llm_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Compute estimated USD cost from input and output token counts."""
    in_price, out_price = MODEL_PRICING_PER_MTOK.get(
        model,
        (
            settings.anthropic_input_price_per_million_tokens,
            settings.anthropic_output_price_per_million_tokens,
        ),
    )
    usd = (input_tokens / 1_000_000) * in_price + (output_tokens / 1_000_000) * out_price
    return round(usd, 6)


async def record_llm_cost(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    est_usd: float,
    article_id: uuid.UUID | None = None,
) -> None:
    """Persist a single LLM invocation cost row to the `costs` table."""
    async with get_session() as session:
        await insert_cost(
            session,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            est_usd=est_usd,
            article_id=article_id,
        )
        await session.commit()


def _extract_json_object(raw_text: str) -> str:
    """Strip optional markdown code fences around JSON output."""
    stripped = raw_text.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```", stripped, re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()
    return stripped


async def call_llm(
    *,
    system_prompt: str,
    user_input: str,
    model: str,
    max_tokens: int = 2048,
    output_schema: type[BaseModel] | None = None,
    article_id: uuid.UUID | None = None,
    persist_cost: bool = False,
) -> dict[str, Any]:
    """Invoke Anthropic Claude (with structured tool output when `output_schema`
    is provided) and return `{"output": ..., "usage": {...}}`.
    """
    llm = get_anthropic_client()
    if llm is None:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not configured; cannot invoke live Anthropic model."
        )

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_input}],
    }

    if output_schema is not None:
        kwargs["tools"] = [
            {
                "name": "structured_output",
                "description": f"Return structured output conforming to {output_schema.__name__}.",
                "input_schema": output_schema.model_json_schema(),
            }
        ]
        kwargs["tool_choice"] = {"type": "tool", "name": "structured_output"}

    response = await llm.messages.create(**kwargs)

    usage_obj = getattr(response, "usage", None)
    input_tokens = int(getattr(usage_obj, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage_obj, "output_tokens", 0) or 0)
    est_usd = estimate_llm_cost_usd(model, input_tokens, output_tokens)

    usage: dict[str, Any] = {
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "est_usd": est_usd,
        "article_id": str(article_id) if article_id is not None else None,
    }

    if persist_cost:
        await record_llm_cost(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            est_usd=est_usd,
            article_id=article_id,
        )

    parsed_output: Any = None
    for block in response.content:
        block_type = getattr(block, "type", None)
        if block_type == "tool_use" and hasattr(block, "input"):
            parsed_output = block.input
            break
        if block_type == "text" and hasattr(block, "text"):
            text_val = str(block.text)
            if output_schema is not None:
                cleaned = _extract_json_object(text_val)
                parsed_output = json.loads(cleaned)
            else:
                parsed_output = text_val
            break

    if output_schema is not None:
        validated = output_schema.model_validate(parsed_output)
        return {"output": validated.model_dump(mode="json"), "usage": usage}

    return {"output": str(parsed_output or ""), "usage": usage}

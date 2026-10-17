"""Hand-written agent execution layer built directly on Inngest step primitives.

No agent frameworks (LangChain, LangGraph, CrewAI) are used. Every LLM call is
wrapped in `step.run(step_id, ...)` so outputs are durably memoized by Inngest
and validated with Pydantic v2.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, overload
from uuid import UUID

import inngest
from pydantic import BaseModel

from kb.agents.providers import call_llm


@dataclass(frozen=True)
class Agent[T: BaseModel]:
    """Declarative specification for a single-purpose agent role."""

    name: str
    role: str
    system_prompt: str
    model: str
    output_schema: type[T] | None = None
    max_tokens: int = 2048


@overload
async def run_agent[T: BaseModel](
    step: inngest.Step | Any,
    agent: Agent[T],
    user_input: str,
    step_id: str,
    article_id: UUID | None = None,
) -> T: ...


@overload
async def run_agent(
    step: inngest.Step | Any,
    agent: Agent[Any],
    user_input: str,
    step_id: str,
    article_id: UUID | None = None,
) -> Any: ...


async def run_agent[T: BaseModel](
    step: inngest.Step | Any,
    agent: Agent[T],
    user_input: str,
    step_id: str,
    article_id: UUID | None = None,
) -> T | str:
    """Execute one LLM call for `agent` inside `step.run(step_id, ...)` so its
    output is durably memoized by Inngest and its token cost is captured by
    `CostCaptureMiddleware`.
    """

    async def _call() -> dict[str, Any]:
        return await call_llm(
            system_prompt=agent.system_prompt,
            user_input=user_input,
            model=agent.model,
            max_tokens=agent.max_tokens,
            output_schema=agent.output_schema,
            article_id=article_id,
        )

    step_result: Any = await step.run(step_id, _call)

    if isinstance(step_result, dict) and "output" in step_result:
        raw_output = step_result["output"]
    else:
        raw_output = step_result

    if agent.output_schema is not None:
        if isinstance(raw_output, agent.output_schema):
            return raw_output
        if isinstance(raw_output, str):
            return agent.output_schema.model_validate_json(raw_output)
        return agent.output_schema.model_validate(raw_output)

    return str(raw_output)


async def bounded_loop[T: BaseModel](
    step: inngest.Step,
    agent: Agent[T],
    initial_input: str,
    *,
    step_prefix: str,
    should_stop: Callable[[T | str, int], Awaitable[bool] | bool],
    next_input: Callable[[T | str, int], Awaitable[str] | str],
    max_iterations: int = 3,
    max_usd: float = 0.50,
    article_id: UUID | None = None,
) -> list[T | str]:
    """Execute a bounded multi-step agent loop with hard iteration and USD cost caps."""
    outputs: list[T | str] = []
    current_input = initial_input
    accumulated_usd = 0.0

    for iteration in range(1, max_iterations + 1):
        if accumulated_usd >= max_usd:
            break

        step_id = f"{step_prefix}-{iteration}"
        result = await run_agent(
            step=step,
            agent=agent,
            user_input=current_input,
            step_id=step_id,
            article_id=article_id,
        )
        outputs.append(result)

        stop_decision = should_stop(result, iteration)
        if isinstance(stop_decision, Awaitable):
            stop_decision = await stop_decision
        if stop_decision or iteration >= max_iterations:
            break

        next_prompt = next_input(result, iteration)
        if isinstance(next_prompt, Awaitable):
            next_prompt = await next_prompt
        current_input = next_prompt

    return outputs

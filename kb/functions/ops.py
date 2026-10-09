"""kb/functions/ops.py

Listens to inngest/function.failed system events and persists them into the
failures table. Captures real failures in production and staging for evaluation
in week 5.
"""

from __future__ import annotations

import json

import inngest

from kb.db.queries import record_failure
from kb.db.session import get_session
from kb.inngest_client import client


@client.create_function(
    fn_id="handle-function-failed",
    name="Record Function Failures",
    trigger=inngest.TriggerEvent(event="inngest/function.failed"),
)
async def handle_function_failed(ctx: inngest.Context) -> dict[str, object]:
    """Capture function failures into the failures table."""
    data = ctx.event.data or {}

    function_id = str(data.get("function_id") or ctx.event.name)
    run_id = str(data.get("run_id") or ctx.event.id)
    raw_error = data.get("error")
    if isinstance(raw_error, dict):
        error_msg = json.dumps(raw_error)
    else:
        error_msg = str(raw_error or "Unknown error")

    event_data = data.get("event")
    if not isinstance(event_data, dict):
        event_data = {"raw": data}

    async with get_session() as session:
        failure_id = await record_failure(
            session,
            function_id=function_id,
            run_id=run_id,
            error=error_msg,
            event_data=event_data,
        )
        await session.commit()

    return {"failure_id": str(failure_id), "status": "recorded"}

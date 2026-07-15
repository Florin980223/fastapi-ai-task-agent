"""Persistent tracing for POST /agent/execute runs.

record_execute_run is called from routes/agent.py's execute() in a
finally block, after the business response has already been fully built.
It deliberately does NOT take the request's own db Session: tracing
writes always go through their own database.SessionLocal() session, on a
separate connection/transaction, so a tracing failure - or even a
tracing rollback - can never affect the request's already-committed task
mutation or the HTTP response already computed. It is a best-effort
operation: any exception here is caught, logged as a warning (never
including secrets - nothing sensitive ever flows through these arguments
in the first place, only the user's message, the configured provider
name, and already tool-schema-validated arguments/results), and
swallowed.

list_runs/find_run back the two read-only GET /agent/runs endpoints and
use the normal request-scoped db session like any other read, since
there's no business operation to isolate from there.
"""

import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import database
from app.db_models import AgentRun, AgentRunStep
from app.schemas import ExecuteResponse

logger = logging.getLogger(__name__)

# Keeps a step's stored result bounded and safe - never a raw, possibly
# large external API response (e.g. get_weather) or a long list_tasks
# result.
_MAX_RESULT_SUMMARY_CHARS = 500


def _json_safe(value: object) -> str:
    """Serialize a value that's already known to be a plain, tool-schema
    -shaped structure (dict/list/str/int/bool/None) into a JSON string.

    Routed through FastAPI's jsonable_encoder first so nothing
    unexpected (an ORM instance, a non-JSON-native type) can ever be
    persisted as-is.
    """
    return json.dumps(jsonable_encoder(value))


def _summarize_result(result: dict | list | None) -> str | None:
    """A bounded, JSON-safe summary of a step's result - the full value
    if it's reasonably small, otherwise a short, fixed-format note
    instead of the raw (possibly large) value.
    """
    if result is None:
        return None

    serialized = _json_safe(result)
    if len(serialized) <= _MAX_RESULT_SUMMARY_CHARS:
        return serialized

    if isinstance(result, list):
        return f"[omitted: {len(result)}-item result too large to store]"
    return f"[omitted: result too large to store ({len(serialized)} chars)]"


def _derive_status(response: ExecuteResponse) -> str:
    """Deterministic status derivation from an already-built ExecuteResponse.

    - success: all executed operations succeeded.
    - partial: at least one multi-step operation succeeded before a
      later error/stopped step.
    - error: the single operation, or the first plan step, failed (this
      also covers a multi-step plan that produced zero steps at all -
      planning was refused or failed before anything could run).
    - clarification_required / confirmation_required / cancelled / no_tool:
      exactly what the response's own fields already say.
    """
    if response.needs_clarification:
        return "clarification_required"
    if response.needs_confirmation:
        return "confirmation_required"

    if response.is_multi_step:
        if not response.steps:
            return "error"
        if all(step.status == "success" for step in response.steps):
            return "success"
        if response.steps[0].status != "success":
            return "error"
        return "partial"

    if response.selected_tool is None:
        # The two cancellation branches are the only producers of this
        # exact reason text anywhere in the codebase.
        if response.reason == "The pending action was cancelled.":
            return "cancelled"
        return "no_tool"

    if isinstance(response.result, dict) and "error" in response.result:
        return "error"
    return "success"


def _derive_error(response: ExecuteResponse, status: str) -> str | None:
    if status == "error":
        if response.is_multi_step:
            if response.steps:
                return response.steps[-1].error
            return response.reason
        if isinstance(response.result, dict):
            return response.result.get("error")
        return None
    if status == "partial" and response.steps:
        return response.steps[-1].error
    return None


def _build_step_records(
    response: ExecuteResponse,
    single_step_arguments: dict | None,
    single_step_duration_ms: int | None,
) -> list[dict]:
    """One record per AgentRunStep to create for this run.

    Multi-step: one per response.steps entry, in order. Single-step: at
    most one, and only when a tool actually executed (single_step_duration_ms
    is only ever set by routes/agent.py right around an
    agent_service.execute_tool call).
    """
    if response.is_multi_step:
        return [
            {
                "step_number": step.step,
                "tool": step.tool,
                "arguments_json": _json_safe(step.arguments),
                "status": step.status,
                "duration_ms": step.duration_ms,
                "error": step.error,
                "result_summary": _summarize_result(step.result),
            }
            for step in response.steps
        ]

    if response.selected_tool is not None and single_step_duration_ms is not None:
        error = response.result.get("error") if isinstance(response.result, dict) else None
        return [
            {
                "step_number": 1,
                "tool": response.selected_tool,
                "arguments_json": _json_safe(single_step_arguments or {}),
                "status": "error" if error else "success",
                "duration_ms": single_step_duration_ms,
                "error": error,
                "result_summary": _summarize_result(response.result),
            }
        ]

    return []


def record_execute_run(
    *,
    run_id: uuid.UUID,
    conversation_id: uuid.UUID,
    message: str,
    decision_provider: str,
    started_at: datetime,
    duration_ms: int,
    response: ExecuteResponse | None,
    single_step_arguments: dict | None = None,
    single_step_duration_ms: int | None = None,
) -> None:
    """Best-effort: persist an AgentRun (+ AgentRunStep rows) on a brand
    new session/transaction, completely separate from the request's own
    db session, so this can never roll back or otherwise affect an
    already-committed task mutation. Never raises.
    """
    if response is None:
        # No branch of execute() finished normally (an unhandled
        # exception propagated before building a response) - nothing to
        # trace.
        return

    trace_db: Session = database.SessionLocal()
    try:
        status = _derive_status(response)
        error = _derive_error(response, status)
        step_records = _build_step_records(response, single_step_arguments, single_step_duration_ms)

        run = AgentRun(
            run_id=run_id,
            conversation_id=conversation_id,
            message=message,
            decision_provider=decision_provider,
            is_multi_step=response.is_multi_step,
            status=status,
            selected_tool=response.selected_tool,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            duration_ms=duration_ms,
            error=error,
        )
        trace_db.add(run)
        for record in step_records:
            trace_db.add(AgentRunStep(run_id=run_id, **record))
        trace_db.commit()
    except Exception as exc:
        # Never expose secrets: only run_id (an opaque UUID we generated)
        # and the exception text are logged - no headers, keys, or env
        # values ever flow through this function's arguments.
        logger.warning("Failed to persist agent run trace for run_id=%s: %s", run_id, exc)
        trace_db.rollback()
    finally:
        trace_db.close()


def list_runs(db: Session, limit: int) -> list[AgentRun]:
    stmt = select(AgentRun).order_by(AgentRun.started_at.desc()).limit(limit)
    return list(db.scalars(stmt).all())


def find_run(db: Session, run_id: uuid.UUID) -> AgentRun | None:
    stmt = select(AgentRun).options(selectinload(AgentRun.steps)).where(AgentRun.run_id == run_id)
    return db.scalar(stmt)

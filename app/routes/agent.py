"""Registry of tools a future AI agent could call.

This is intentionally static: it just describes the HTTP endpoints
that already exist in this API (tasks + weather), so an agent (or a
developer) can discover what actions are available without reading
the source code.
"""

import logging
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import (
    AgentRunDetailResponse,
    AgentRunSummaryResponse,
    ClarificationOptionResponse,
    DecideToolRequest,
    DecideToolResponse,
    ExecuteRequest,
    ExecuteResponse,
    StepResultResponse,
    ToolResponse,
)
from app.services import (
    agent_decision,
    agent_planner,
    agent_service,
    agent_trace_service,
    clarification,
    conversation_memory,
    rate_limiter,
    task_resolution,
    task_service,
    tool_schemas,
)
from app.services.auth import AuthenticatedUser, get_current_user
from app.services.conversation_memory import PendingClarification, PendingConfirmation
from app.services.tool_decision import ToolDecision
from app.services.tool_registry import AVAILABLE_TOOLS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent"])


@router.get("/tools", response_model=list[ToolResponse])
def get_tools():
    return AVAILABLE_TOOLS


@router.post("/decide-tool", response_model=DecideToolResponse)
def decide_tool(request: DecideToolRequest):
    try:
        decision = agent_decision.decide_tool(request.message)
    except agent_decision.UnsafeFallbackError as exc:
        return DecideToolResponse(message=request.message, selected_tool=None, reason=str(exc))
    return DecideToolResponse(
        message=request.message,
        selected_tool=decision.selected_tool,
        reason=decision.reason,
    )


@router.get("/runs", response_model=list[AgentRunSummaryResponse])
def list_runs(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    return agent_trace_service.list_runs(db, limit=limit, user_id=current_user.user_id)


@router.get("/runs/{run_id}", response_model=AgentRunDetailResponse)
def get_run(run_id: uuid.UUID, db: Session = Depends(get_db), current_user: AuthenticatedUser = Depends(get_current_user)):
    run = agent_trace_service.find_run(db, run_id, user_id=current_user.user_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


def _lookup_existing_task_title(db: Session, user_id: str, decision: ToolDecision) -> str | None:
    """Best-effort title lookup for a decision that already carries an
    explicit task_id (never for a task_title reference - that's already
    handled by task_resolution.resolve_task_title_argument, which also
    resolves the title). Used only to populate
    ExecuteResponse.resolved_task_title as optional display context -
    never affects which task actually gets acted on, since execution
    always reads decision.arguments["task_id"] directly.
    """
    if decision.selected_tool not in ("mark_task_done", "update_task", "delete_task"):
        return None
    task_id = decision.arguments.get("task_id")
    if task_id is None:
        return None
    task = task_service.find_task(db, user_id, task_id)
    return task.title if task is not None else None


@router.post("/execute", response_model=ExecuteResponse)
def execute(
    request: ExecuteRequest,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(rate_limiter.enforce_execute_rate_limit),
):
    # Every HTTP request gets its own run_id and its own persistent trace
    # - even a follow-up reply ("yes", a clarification answer) on the same
    # conversation_id. See agent_trace_service.record_execute_run, called
    # unconditionally below regardless of which branch returns.
    run_id = uuid.uuid4()
    started_at = datetime.now(timezone.utc)
    started = time.monotonic()
    single_step_arguments: dict | None = None
    single_step_duration_ms: int | None = None
    response: ExecuteResponse | None = None

    conversation_id = request.conversation_id or uuid.uuid4()
    user_id = current_user.user_id
    message = request.message

    try:
        pending_confirmation = conversation_memory.peek_confirmation(db, user_id, conversation_id)

        if pending_confirmation is not None:
            if clarification.is_confirmation_cancellation(message):
                conversation_memory.clear_confirmation(db, user_id, conversation_id)
                response = ExecuteResponse(
                    run_id=run_id,
                    conversation_id=conversation_id,
                    message=message,
                    selected_tool=None,
                    result=None,
                    reason="The pending action was cancelled.",
                    final_answer="Okay, I cancelled the pending action.",
                    needs_clarification=False,
                    clarification_question=None,
                    needs_confirmation=False,
                    confirmation_question=None,
                    is_multi_step=False,
                    steps=[],
                )
                return response

            if not clarification.is_confirmation_reply(message):
                # Neither a clear yes nor a clear no - keep waiting rather
                # than guessing, and don't touch any state.
                response = ExecuteResponse(
                    run_id=run_id,
                    conversation_id=conversation_id,
                    message=message,
                    selected_tool=pending_confirmation.selected_tool,
                    result=None,
                    reason=pending_confirmation.reason,
                    final_answer=pending_confirmation.question,
                    needs_clarification=False,
                    clarification_question=None,
                    needs_confirmation=True,
                    confirmation_question=pending_confirmation.question,
                    is_multi_step=False,
                    steps=[],
                )
                return response

            # A clear "yes" - atomically consume the confirmation before
            # executing anything, so a duplicate/concurrent "yes" can
            # never execute the destructive tool twice (see
            # conversation_memory.consume_confirmation). Never execute
            # based on the pending_confirmation peeked above - only the
            # freshly consumed value.
            consumed = conversation_memory.consume_confirmation(db, user_id, conversation_id)
            if consumed is not None:
                decision = ToolDecision(
                    selected_tool=consumed.selected_tool,
                    arguments=consumed.arguments,
                    reason=consumed.reason,
                )
                tool_schemas.validate_tool_call(decision.selected_tool, decision.arguments)
                # Looked up before execute_tool runs - delete_task removes
                # the row, so the title would be gone afterwards.
                consumed_resolved_task_title = _lookup_existing_task_title(db, user_id, decision)
                step_started = time.monotonic()
                result = agent_service.execute_tool(decision, db, user_id)
                single_step_duration_ms = int((time.monotonic() - step_started) * 1000)
                single_step_arguments = decision.arguments
                conversation_memory.record_result(user_id, conversation_id, decision.selected_tool, result)
                final_answer = agent_service.generate_final_answer(decision.selected_tool, result)
                response = ExecuteResponse(
                    run_id=run_id,
                    conversation_id=conversation_id,
                    message=message,
                    selected_tool=decision.selected_tool,
                    result=result,
                    resolved_task_title=consumed_resolved_task_title,
                    reason=decision.reason,
                    final_answer=final_answer,
                    needs_clarification=False,
                    clarification_question=None,
                    needs_confirmation=False,
                    confirmation_question=None,
                    is_multi_step=False,
                    steps=[],
                )
                return response
            # else: the confirmation expired, or a concurrent/duplicate
            # "yes" already consumed it, between the peek above and this
            # consume attempt. Fall through and treat this message as an
            # ordinary new one - never execute anything on a stale peek.

        pending = conversation_memory.get(db, user_id, conversation_id)

        if pending is not None:
            if clarification.is_cancellation(message):
                conversation_memory.clear(db, user_id, conversation_id)
                response = ExecuteResponse(
                    run_id=run_id,
                    conversation_id=conversation_id,
                    message=message,
                    selected_tool=None,
                    result=None,
                    reason="The pending action was cancelled.",
                    final_answer="Okay, I cancelled the pending action.",
                    needs_clarification=False,
                    clarification_question=None,
                    needs_confirmation=False,
                    confirmation_question=None,
                    is_multi_step=False,
                    steps=[],
                )
                return response

            # A pending clarification exists - parse this reply deterministically
            # and merge it in. No LLM provider is consulted for a bare reply
            # like "3". An ambiguous-title clarification (carrying a candidate
            # list) is parsed differently from an ordinary missing-argument
            # one - see clarification.is_ambiguous_task_clarification.
            if clarification.is_ambiguous_task_clarification(pending):
                merged_arguments = clarification.merge_ambiguous_task_reply(pending, message)
                if clarification.CLARIFICATION_CANDIDATES_KEY in merged_arguments:
                    # The reply didn't clearly pick one of the candidates -
                    # keep waiting rather than guessing. Re-ask the same
                    # question with the same candidates; nothing is
                    # re-persisted since the pending state is unchanged.
                    candidates = clarification.candidates_from_pending(pending)
                    question = clarification.build_ambiguous_task_question(candidates)
                    response = ExecuteResponse(
                        run_id=run_id,
                        conversation_id=conversation_id,
                        message=message,
                        selected_tool=pending.selected_tool,
                        result=None,
                        reason=pending.reason,
                        final_answer=question,
                        needs_clarification=True,
                        clarification_question=question,
                        clarification_options=[ClarificationOptionResponse(task_id=c.task_id, title=c.title) for c in candidates],
                        needs_confirmation=False,
                        confirmation_question=None,
                        is_multi_step=False,
                        steps=[],
                    )
                    return response
            else:
                merged_arguments = clarification.merge_reply(pending, message)

            decision = ToolDecision(
                selected_tool=pending.selected_tool,
                arguments=merged_arguments,
                reason=pending.reason,
            )
        else:
            if agent_planner.should_attempt_planning(message):
                plan = agent_planner.decide_plan(message)
                if plan is not None:
                    step_results = agent_planner.execute_plan(plan, db, conversation_id, user_id)
                    response = ExecuteResponse(
                        run_id=run_id,
                        conversation_id=conversation_id,
                        message=message,
                        selected_tool=None,
                        result=None,
                        reason=agent_planner.build_plan_reason(step_results, len(plan.steps)),
                        final_answer=agent_planner.build_plan_final_answer(step_results),
                        needs_clarification=False,
                        clarification_question=None,
                        needs_confirmation=False,
                        confirmation_question=None,
                        is_multi_step=True,
                        steps=[StepResultResponse(**r.model_dump()) for r in step_results],
                    )
                    return response
                # Planning was attempted (the message looked multi-step) but
                # failed or produced an invalid/disallowed plan. Do NOT fall
                # back to deciding a single tool from this (possibly
                # multi-action) message - that could silently execute only a
                # fragment of what was asked. Stop cleanly instead.
                response = ExecuteResponse(
                    run_id=run_id,
                    conversation_id=conversation_id,
                    message=message,
                    selected_tool=None,
                    result=None,
                    reason="Multi-step planning did not produce a valid, safe plan.",
                    final_answer="I couldn't create a safe multi-step plan. Please rephrase the request.",
                    needs_clarification=False,
                    clarification_question=None,
                    needs_confirmation=False,
                    confirmation_question=None,
                    is_multi_step=True,
                    steps=[],
                )
                return response
            try:
                decision = agent_decision.decide_tool(message)
            except agent_decision.UnsafeFallbackError as exc:
                # A configured LLM provider failed and it wasn't judged
                # safe to fall back to rule_based for this message (see
                # agent_decision._safe_to_fall_back) - fail cleanly instead
                # of guessing, mirroring the multi-step planning-failure
                # response shape above. Never executes anything.
                response = ExecuteResponse(
                    run_id=run_id,
                    conversation_id=conversation_id,
                    message=message,
                    selected_tool=None,
                    result=None,
                    reason=str(exc),
                    final_answer=str(exc),
                    needs_clarification=False,
                    clarification_question=None,
                    needs_confirmation=False,
                    confirmation_question=None,
                    is_multi_step=False,
                    steps=[],
                )
                return response

        # Fill in a missing task_id from remembered context, but only if the
        # message explicitly refers back to a prior task ("it", "that task").
        # A generic incomplete request still asks for clarification below.
        last_task_id = conversation_memory.get_last_task_id(db, user_id, conversation_id)
        clarification.resolve_remembered_task_id(decision, last_task_id, message)

        # Resolve a task_title reference (e.g. "the portfolio task") into
        # a task_id before anything downstream ever runs. A no-op with no
        # database query at all whenever task_id is already present (a
        # digit-containing message, or context-resolved above) - see
        # task_resolution.resolve_task_title_argument. Must run before
        # missing_arguments (so a resolved task_id is never reported
        # missing) and before the destructive-confirmation gate further
        # below (so an ambiguous/not-found delete-by-title can never
        # reach it - see tests/test_agent_execute.py).
        resolution_outcome = task_resolution.resolve_task_title_argument(decision, db, user_id)
        # title_resolved_task_title is None whenever task_id came from an
        # explicit reference rather than a title match - this is the exact,
        # pre-existing value clarification.build_confirmation_question
        # takes below, and its wording (with vs. without a named title) must
        # stay byte-identical to before for an explicit task_id (see
        # tests/test_confirmation.py's plain "delete task #N?" wording).
        title_resolved_task_title = resolution_outcome.title if resolution_outcome.status == "resolved" else None
        # resolved_task_title is the broader value ExecuteResponse exposes:
        # the same title-resolved value, or - only when that's absent - a
        # best-effort lookup from an explicit task_id, purely as optional
        # display context (see _lookup_existing_task_title). Never fed into
        # build_confirmation_question, so confirmation wording/safety never
        # depends on it.
        resolved_task_title = title_resolved_task_title
        if resolved_task_title is None:
            resolved_task_title = _lookup_existing_task_title(db, user_id, decision)
        if resolution_outcome.status in ("ambiguous", "not_found"):
            if resolution_outcome.status == "ambiguous":
                candidates = list(resolution_outcome.candidates)
                question = clarification.build_ambiguous_task_question(candidates)
                stored_arguments = dict(decision.arguments)
                stored_arguments[clarification.CLARIFICATION_CANDIDATES_KEY] = [
                    {"task_id": c.task_id, "title": c.title} for c in candidates
                ]
                clarification_options = [ClarificationOptionResponse(task_id=c.task_id, title=c.title) for c in candidates]
            else:
                question = clarification.build_not_found_task_question()
                stored_arguments = dict(decision.arguments)
                clarification_options = None

            try:
                conversation_memory.set(
                    db,
                    user_id,
                    conversation_id,
                    PendingClarification(
                        selected_tool=decision.selected_tool,
                        arguments=stored_arguments,
                        reason=decision.reason,
                        missing=["task_id"],
                    ),
                )
            except Exception as exc:
                logger.warning(
                    "Failed to persist pending title-resolution clarification for user_id=%s conversation_id=%s: %s",
                    user_id,
                    conversation_id,
                    exc,
                )
                raise HTTPException(status_code=500, detail="Failed to save conversation state. Please try again.") from exc

            response = ExecuteResponse(
                run_id=run_id,
                conversation_id=conversation_id,
                message=message,
                selected_tool=decision.selected_tool,
                result=None,
                reason=decision.reason,
                final_answer=question,
                needs_clarification=True,
                clarification_question=question,
                clarification_options=clarification_options,
                needs_confirmation=False,
                confirmation_question=None,
                is_multi_step=False,
                steps=[],
            )
            return response

        missing = clarification.missing_arguments(decision)
        if missing:
            question = clarification.build_clarification_question(decision.selected_tool, missing)
            try:
                conversation_memory.set(
                    db,
                    user_id,
                    conversation_id,
                    PendingClarification(
                        selected_tool=decision.selected_tool,
                        arguments=decision.arguments,
                        reason=decision.reason,
                        missing=missing,
                    ),
                )
            except Exception as exc:
                # Never claim needs_clarification=true unless the pending
                # state actually made it to disk - see
                # conversation_memory.py's module docstring. Never expose
                # db details/secrets, only a generic message; the real
                # exception is logged server-side only.
                logger.warning(
                    "Failed to persist pending clarification for user_id=%s conversation_id=%s: %s",
                    user_id,
                    conversation_id,
                    exc,
                )
                raise HTTPException(status_code=500, detail="Failed to save conversation state. Please try again.") from exc
            response = ExecuteResponse(
                run_id=run_id,
                conversation_id=conversation_id,
                message=message,
                selected_tool=decision.selected_tool,
                result=None,
                reason=clarification.build_reason(decision.selected_tool, missing),
                final_answer=question,
                needs_clarification=True,
                clarification_question=question,
                needs_confirmation=False,
                confirmation_question=None,
                is_multi_step=False,
                steps=[],
            )
            return response

        # Complete decision - validate it the same way a provider decision
        # would be (skipped when no tool was selected at all, since that's
        # not a real tool call to validate).
        if decision.selected_tool is not None:
            tool_schemas.validate_tool_call(decision.selected_tool, decision.arguments)

        # The decision is fully resolved now (no missing arguments), whether
        # it goes on to execute immediately or wait for confirmation below -
        # either way, any pending clarification for this conversation is done.
        conversation_memory.clear(db, user_id, conversation_id)

        # Destructive tools don't execute yet - park the decision and ask
        # the user to explicitly confirm it first.
        if clarification.requires_confirmation(decision.selected_tool):
            question = clarification.build_confirmation_question(decision, title_resolved_task_title)
            try:
                conversation_memory.set_confirmation(
                    db,
                    user_id,
                    conversation_id,
                    PendingConfirmation(
                        selected_tool=decision.selected_tool,
                        arguments=decision.arguments,
                        reason=decision.reason,
                        question=question,
                    ),
                )
            except Exception as exc:
                # Never claim needs_confirmation=true unless the pending
                # state actually made it to disk - a lost confirmation
                # would mean a later "yes" falls through to an ordinary
                # message instead of executing the destructive tool, but
                # a *falsely claimed* pending confirmation would be worse
                # (the user thinks they still need to confirm something
                # that was never actually parked). Never expose db
                # details/secrets, only a generic message; the real
                # exception is logged server-side only.
                logger.warning(
                    "Failed to persist pending confirmation for user_id=%s conversation_id=%s: %s",
                    user_id,
                    conversation_id,
                    exc,
                )
                raise HTTPException(status_code=500, detail="Failed to save conversation state. Please try again.") from exc
            response = ExecuteResponse(
                run_id=run_id,
                conversation_id=conversation_id,
                message=message,
                selected_tool=decision.selected_tool,
                result=None,
                resolved_task_title=resolved_task_title,
                reason=decision.reason,
                final_answer=question,
                needs_clarification=False,
                clarification_question=None,
                needs_confirmation=True,
                confirmation_question=question,
                is_multi_step=False,
                steps=[],
            )
            return response

        step_started = time.monotonic()
        result = agent_service.execute_tool(decision, db, user_id)
        single_step_duration_ms = int((time.monotonic() - step_started) * 1000)
        single_step_arguments = decision.arguments
        conversation_memory.record_result(user_id, conversation_id, decision.selected_tool, result)
        final_answer = agent_service.generate_final_answer(decision.selected_tool, result)
        response = ExecuteResponse(
            run_id=run_id,
            conversation_id=conversation_id,
            message=message,
            selected_tool=decision.selected_tool,
            result=result,
            resolved_task_title=resolved_task_title,
            reason=decision.reason,
            final_answer=final_answer,
            needs_clarification=False,
            clarification_question=None,
            needs_confirmation=False,
            confirmation_question=None,
            is_multi_step=False,
            steps=[],
        )
        return response
    finally:
        agent_trace_service.record_execute_run(
            run_id=run_id,
            conversation_id=conversation_id,
            user_id=user_id,
            message=message,
            decision_provider=agent_decision.DECISION_PROVIDER,
            started_at=started_at,
            duration_ms=int((time.monotonic() - started) * 1000),
            response=response,
            single_step_arguments=single_step_arguments,
            single_step_duration_ms=single_step_duration_ms,
        )

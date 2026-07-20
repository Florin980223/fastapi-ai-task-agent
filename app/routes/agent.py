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
    DecideToolRequest,
    DecideToolResponse,
    ExecuteRequest,
    ExecuteResponse,
    StepResultResponse,
    ToolResponse,
)
from app.services import agent_decision, agent_planner, agent_service, agent_trace_service, clarification, conversation_memory, tool_schemas
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
    decision = agent_decision.decide_tool(request.message)
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


@router.post("/execute", response_model=ExecuteResponse)
def execute(request: ExecuteRequest, db: Session = Depends(get_db), current_user: AuthenticatedUser = Depends(get_current_user)):
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
            # like "3".
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
            decision = agent_decision.decide_tool(message)

        # Fill in a missing task_id from remembered context, but only if the
        # message explicitly refers back to a prior task ("it", "that task").
        # A generic incomplete request still asks for clarification below.
        last_task_id = conversation_memory.get_last_task_id(db, user_id, conversation_id)
        clarification.resolve_remembered_task_id(decision, last_task_id, message)

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
            question = clarification.build_confirmation_question(decision)
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

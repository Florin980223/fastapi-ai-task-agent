"""Registry of tools a future AI agent could call.

This is intentionally static: it just describes the HTTP endpoints
that already exist in this API (tasks + weather), so an agent (or a
developer) can discover what actions are available without reading
the source code.
"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import DecideToolRequest, DecideToolResponse, ExecuteRequest, ExecuteResponse, StepResultResponse, ToolResponse
from app.services import agent_decision, agent_planner, agent_service, clarification, conversation_memory, tool_schemas
from app.services.conversation_memory import PendingClarification, PendingConfirmation
from app.services.tool_decision import ToolDecision
from app.services.tool_registry import AVAILABLE_TOOLS

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


@router.post("/execute", response_model=ExecuteResponse)
def execute(request: ExecuteRequest, db: Session = Depends(get_db)):
    conversation_id = request.conversation_id or uuid.uuid4()
    message = request.message

    pending_confirmation = conversation_memory.get_confirmation(conversation_id)

    if pending_confirmation is not None:
        if clarification.is_confirmation_cancellation(message):
            conversation_memory.clear_confirmation(conversation_id)
            return ExecuteResponse(
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

        if not clarification.is_confirmation_reply(message):
            # Neither a clear yes nor a clear no - keep waiting rather
            # than guessing, and don't touch any state.
            return ExecuteResponse(
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

        # Confirmed - revalidate the stored decision immediately before
        # executing it, then run it exactly like any other complete
        # decision below.
        conversation_memory.clear_confirmation(conversation_id)
        decision = ToolDecision(
            selected_tool=pending_confirmation.selected_tool,
            arguments=pending_confirmation.arguments,
            reason=pending_confirmation.reason,
        )
        tool_schemas.validate_tool_call(decision.selected_tool, decision.arguments)
        result = agent_service.execute_tool(decision, db)
        conversation_memory.record_result(conversation_id, decision.selected_tool, result)
        final_answer = agent_service.generate_final_answer(decision.selected_tool, result)
        return ExecuteResponse(
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

    pending = conversation_memory.get(conversation_id)

    if pending is not None:
        if clarification.is_cancellation(message):
            conversation_memory.clear(conversation_id)
            return ExecuteResponse(
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
                step_results = agent_planner.execute_plan(plan, db, conversation_id)
                return ExecuteResponse(
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
            # Planning was attempted (the message looked multi-step) but
            # failed or produced an invalid/disallowed plan. Do NOT fall
            # back to deciding a single tool from this (possibly
            # multi-action) message - that could silently execute only a
            # fragment of what was asked. Stop cleanly instead.
            return ExecuteResponse(
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
        decision = agent_decision.decide_tool(message)

    # Fill in a missing task_id from remembered context, but only if the
    # message explicitly refers back to a prior task ("it", "that task").
    # A generic incomplete request still asks for clarification below.
    last_task_id = conversation_memory.get_last_task_id(conversation_id)
    clarification.resolve_remembered_task_id(decision, last_task_id, message)

    missing = clarification.missing_arguments(decision)
    if missing:
        question = clarification.build_clarification_question(decision.selected_tool, missing)
        conversation_memory.set(
            conversation_id,
            PendingClarification(
                selected_tool=decision.selected_tool,
                arguments=decision.arguments,
                reason=decision.reason,
                missing=missing,
            ),
        )
        return ExecuteResponse(
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

    # Complete decision - validate it the same way a provider decision
    # would be (skipped when no tool was selected at all, since that's
    # not a real tool call to validate).
    if decision.selected_tool is not None:
        tool_schemas.validate_tool_call(decision.selected_tool, decision.arguments)

    # The decision is fully resolved now (no missing arguments), whether
    # it goes on to execute immediately or wait for confirmation below -
    # either way, any pending clarification for this conversation is done.
    conversation_memory.clear(conversation_id)

    # Destructive tools don't execute yet - park the decision and ask
    # the user to explicitly confirm it first.
    if clarification.requires_confirmation(decision.selected_tool):
        question = clarification.build_confirmation_question(decision)
        conversation_memory.set_confirmation(
            conversation_id,
            PendingConfirmation(
                selected_tool=decision.selected_tool,
                arguments=decision.arguments,
                reason=decision.reason,
                question=question,
            ),
        )
        return ExecuteResponse(
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

    result = agent_service.execute_tool(decision, db)
    conversation_memory.record_result(conversation_id, decision.selected_tool, result)
    final_answer = agent_service.generate_final_answer(decision.selected_tool, result)
    return ExecuteResponse(
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

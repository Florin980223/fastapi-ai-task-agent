"""Registry of tools a future AI agent could call.

This is intentionally static: it just describes the HTTP endpoints
that already exist in this API (tasks + weather), so an agent (or a
developer) can discover what actions are available without reading
the source code.
"""

import uuid

from fastapi import APIRouter

from app.schemas import DecideToolRequest, DecideToolResponse, ExecuteRequest, ExecuteResponse, ToolResponse
from app.services import agent_decision, agent_service, clarification, conversation_memory, tool_schemas
from app.services.conversation_memory import PendingClarification
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
def execute(request: ExecuteRequest):
    conversation_id = request.conversation_id or uuid.uuid4()
    message = request.message

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
        decision = agent_decision.decide_tool(message)

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
        )

    # Complete decision - validate it the same way a provider decision
    # would be (skipped when no tool was selected at all, since that's
    # not a real tool call to validate).
    if decision.selected_tool is not None:
        tool_schemas.validate_tool_call(decision.selected_tool, decision.arguments)

    conversation_memory.clear(conversation_id)
    result = agent_service.execute_tool(decision)
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
    )

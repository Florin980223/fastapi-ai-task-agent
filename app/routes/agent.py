"""Registry of tools a future AI agent could call.

This is intentionally static: it just describes the HTTP endpoints
that already exist in this API (tasks + weather), so an agent (or a
developer) can discover what actions are available without reading
the source code.
"""

from fastapi import APIRouter

from app.schemas import DecideToolRequest, DecideToolResponse, ExecuteResponse, ToolResponse
from app.services import agent_decision, agent_service, clarification
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
def execute(request: DecideToolRequest):
    decision = agent_decision.decide_tool(request.message)

    missing = clarification.missing_arguments(decision)
    if missing:
        question = clarification.build_clarification_question(decision.selected_tool, missing)
        return ExecuteResponse(
            message=request.message,
            selected_tool=decision.selected_tool,
            result=None,
            reason=clarification.build_reason(decision.selected_tool, missing),
            final_answer=question,
            needs_clarification=True,
            clarification_question=question,
        )

    result = agent_service.execute_tool(decision)
    final_answer = agent_service.generate_final_answer(decision.selected_tool, result)
    return ExecuteResponse(
        message=request.message,
        selected_tool=decision.selected_tool,
        result=result,
        reason=decision.reason,
        final_answer=final_answer,
        needs_clarification=False,
        clarification_question=None,
    )

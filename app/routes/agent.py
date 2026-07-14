"""Registry of tools a future AI agent could call.

This is intentionally static: it just describes the HTTP endpoints
that already exist in this API (tasks + weather), so an agent (or a
developer) can discover what actions are available without reading
the source code.
"""

from fastapi import APIRouter

from app.schemas import DecideToolRequest, DecideToolResponse, ExecuteResponse, ToolResponse
from app.services import agent_service

router = APIRouter(prefix="/agent", tags=["agent"])

AVAILABLE_TOOLS: list[ToolResponse] = [
    ToolResponse(
        name="create_task",
        description="Create a new task",
        method="POST",
        endpoint="/tasks",
    ),
    ToolResponse(
        name="list_tasks",
        description="List all tasks or filter by completion status",
        method="GET",
        endpoint="/tasks",
    ),
    ToolResponse(
        name="get_task",
        description="Get a single task by id",
        method="GET",
        endpoint="/tasks/{task_id}",
    ),
    ToolResponse(
        name="update_task",
        description="Update a task title and/or description",
        method="PATCH",
        endpoint="/tasks/{task_id}",
    ),
    ToolResponse(
        name="mark_task_done",
        description="Mark a task as completed",
        method="PATCH",
        endpoint="/tasks/{task_id}/done",
    ),
    ToolResponse(
        name="delete_task",
        description="Delete a task by id",
        method="DELETE",
        endpoint="/tasks/{task_id}",
    ),
    ToolResponse(
        name="get_weather",
        description="Get current weather for a city",
        method="GET",
        endpoint="/integrations/weather",
    ),
]


@router.get("/tools", response_model=list[ToolResponse])
def get_tools():
    return AVAILABLE_TOOLS


@router.post("/decide-tool", response_model=DecideToolResponse)
def decide_tool(request: DecideToolRequest):
    selected_tool, reason = agent_service.decide_tool(request.message)
    return DecideToolResponse(
        message=request.message,
        selected_tool=selected_tool,
        reason=reason,
    )


@router.post("/execute", response_model=ExecuteResponse)
def execute(request: DecideToolRequest):
    selected_tool, reason = agent_service.decide_tool(request.message)
    result = agent_service.execute_tool(request.message, selected_tool)
    final_answer = agent_service.generate_final_answer(selected_tool, result)
    return ExecuteResponse(
        message=request.message,
        selected_tool=selected_tool,
        result=result,
        reason=reason,
        final_answer=final_answer,
    )

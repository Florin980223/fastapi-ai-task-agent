"""Registry of tools a future AI agent could call.

This is intentionally static: it just describes the HTTP endpoints
that already exist in this API (tasks + weather), so an agent (or a
developer) can discover what actions are available without reading
the source code. Used by GET /agent/tools, and reused by the Anthropic
decision provider to build Claude's tool definitions (so tool names
and descriptions are defined in exactly one place).
"""

from app.schemas import ToolResponse

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

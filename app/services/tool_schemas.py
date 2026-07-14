"""Shared argument schemas and validation for LLM-based decision providers.

Both the Anthropic and Ollama providers need the same information for
each executable tool: a JSON Schema describing its arguments (for the
provider's native tool-calling request) and the same rules for
validating whatever the model calls back with. Keeping both here means
the two providers can't silently drift apart on what's "required" or
"the right type" for a given tool.
"""

# JSON Schema arguments for each executable tool. This is provider-neutral:
# Anthropic wraps it as a tool's "input_schema", Ollama/OpenAI-style wraps
# it as a function's "parameters" - the schema itself is identical either way.
TOOL_ARGUMENT_SCHEMAS: dict[str, dict] = {
    "create_task": {
        "type": "object",
        "properties": {"title": {"type": "string", "description": "The task title."}},
        "required": ["title"],
    },
    "list_tasks": {
        "type": "object",
        "properties": {
            "done": {
                "type": ["boolean", "null"],
                "description": "Filter by completion status. Omit or null for all tasks.",
            }
        },
    },
    "get_weather": {
        "type": "object",
        "properties": {"city": {"type": "string", "description": "The city to get the weather for."}},
        "required": ["city"],
    },
    "mark_task_done": {
        "type": "object",
        "properties": {"task_id": {"type": "integer", "description": "The id of the task to mark done."}},
        "required": ["task_id"],
    },
    "update_task": {
        "type": "object",
        "properties": {
            "task_id": {"type": "integer", "description": "The id of the task to update."},
            "title": {"type": "string", "description": "The new title for the task."},
        },
        "required": ["task_id", "title"],
    },
    "delete_task": {
        "type": "object",
        "properties": {"task_id": {"type": "integer", "description": "The id of the task to delete."}},
        "required": ["task_id"],
    },
}

# Required arguments and their expected Python types, used to validate a
# model's tool call before trusting it.
REQUIRED_ARGUMENTS: dict[str, dict[str, type]] = {
    "create_task": {"title": str},
    "list_tasks": {},
    "get_weather": {"city": str},
    "mark_task_done": {"task_id": int},
    "update_task": {"task_id": int, "title": str},
    "delete_task": {"task_id": int},
}


class ToolCallValidationError(Exception):
    """Raised when a model's tool call fails validation.

    Each provider catches this and re-raises it as its own
    provider-specific error, which is what agent_decision.decide_tool
    actually catches to trigger a fallback.
    """


def validate_tool_call(tool_name: str | None, arguments: object) -> None:
    """Validate a model-selected tool name and arguments.

    Checks: the tool name is one of the executable tools, arguments is
    a dict, and every required argument for that tool is present with
    a reasonable type. Raises ToolCallValidationError on any problem.
    """
    if tool_name not in REQUIRED_ARGUMENTS:
        raise ToolCallValidationError(f"Model selected an unknown tool: {tool_name!r}.")

    if not isinstance(arguments, dict):
        raise ToolCallValidationError(f"Model returned non-dict arguments for '{tool_name}'.")

    for arg_name, expected_type in REQUIRED_ARGUMENTS[tool_name].items():
        if arg_name not in arguments:
            raise ToolCallValidationError(f"Model's call to '{tool_name}' is missing required argument '{arg_name}'.")
        if not isinstance(arguments[arg_name], expected_type):
            raise ToolCallValidationError(f"Model's call to '{tool_name}' has the wrong type for '{arg_name}'.")

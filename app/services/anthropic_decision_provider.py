"""A Claude-powered "decision provider" for the agent.

Like the rule-based provider (agent_decision.py), this only decides
WHICH tool to use and WHAT arguments to call it with - it returns a
ToolDecision and never runs anything itself. Claude is only ever asked
to select one of the app's own tools via native tool use; it cannot
run arbitrary code, shell commands, or HTTP requests. The caller
(agent_decision.decide_tool) is responsible for validating this
provider's output and falling back to the rule-based provider if
anything here fails.
"""

import anthropic

from app.config import ANTHROPIC_MODEL
from app.services.tool_decision import ToolDecision
from app.services.tool_registry import AVAILABLE_TOOLS

_SYSTEM_PROMPT = (
    "You are the decision-making layer of a small task-management assistant. "
    "Given the user's message, decide which single tool (if any) should handle "
    "it, and call that tool with appropriate arguments. If no tool fits the "
    "message, don't call any tool."
)

# JSON Schema arguments for each executable tool. This is the one piece of
# information that doesn't already exist in tool_registry.py (which only
# has name/description/method/endpoint) - everything else below is reused
# from there, so tool descriptions aren't duplicated.
_INPUT_SCHEMAS: dict[str, dict] = {
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

# Required arguments and their expected Python types, used to validate
# Claude's tool call before trusting it.
_REQUIRED_ARGUMENTS: dict[str, dict[str, type]] = {
    "create_task": {"title": str},
    "list_tasks": {},
    "get_weather": {"city": str},
    "mark_task_done": {"task_id": int},
    "update_task": {"task_id": int, "title": str},
    "delete_task": {"task_id": int},
}


class AnthropicDecisionError(Exception):
    """Raised whenever the Anthropic provider can't produce a trustworthy decision.

    Covers API/network failures, missing tool calls, unknown tools, and
    invalid arguments - agent_decision.decide_tool catches this one type
    and falls back to the rule-based provider.
    """


def _build_claude_tools() -> list[dict]:
    """Build Claude's native tool definitions from the existing tool registry.

    Only tools with an argument schema above are included, which limits
    Claude to exactly the 6 executable tools (e.g. get_task is excluded -
    it has no schema, since this agent doesn't execute it).
    """
    tools = []
    for tool in AVAILABLE_TOOLS:
        if tool.name not in _INPUT_SCHEMAS:
            continue
        tools.append(
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": _INPUT_SCHEMAS[tool.name],
            }
        )
    return tools


def _get_client() -> anthropic.Anthropic:
    """Construct the Anthropic client. Kept as its own function so tests
    can monkeypatch it instead of making a real API call."""
    return anthropic.Anthropic(timeout=10.0)


def _parse_tool_use(block) -> ToolDecision:
    tool_name = getattr(block, "name", None)
    if tool_name not in _REQUIRED_ARGUMENTS:
        raise AnthropicDecisionError(f"Claude selected an unknown tool: {tool_name!r}.")

    arguments = getattr(block, "input", None)
    if not isinstance(arguments, dict):
        raise AnthropicDecisionError(f"Claude returned non-dict arguments for '{tool_name}'.")

    for arg_name, expected_type in _REQUIRED_ARGUMENTS[tool_name].items():
        if arg_name not in arguments:
            raise AnthropicDecisionError(f"Claude's call to '{tool_name}' is missing required argument '{arg_name}'.")
        if not isinstance(arguments[arg_name], expected_type):
            raise AnthropicDecisionError(f"Claude's call to '{tool_name}' has the wrong type for '{arg_name}'.")

    return ToolDecision(
        selected_tool=tool_name,
        arguments=arguments,
        reason=f"Claude (model {ANTHROPIC_MODEL}) selected the '{tool_name}' tool for this message.",
    )


def decide_tool(message: str) -> ToolDecision:
    """Ask Claude to pick a tool (and arguments) for the given message.

    Raises AnthropicDecisionError if the request fails, Claude doesn't
    call a tool, or the tool call is invalid - the caller is expected
    to fall back to the rule-based provider in that case.
    """
    try:
        client = _get_client()
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            tools=_build_claude_tools(),
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            messages=[{"role": "user", "content": message}],
        )
    except Exception as exc:  # broad on purpose: any failure here must trigger a safe fallback
        raise AnthropicDecisionError(f"Anthropic API request failed: {exc}") from exc

    tool_use_block = next((block for block in response.content if getattr(block, "type", None) == "tool_use"), None)
    if tool_use_block is None:
        raise AnthropicDecisionError("Claude did not call a tool for this message.")

    return _parse_tool_use(tool_use_block)

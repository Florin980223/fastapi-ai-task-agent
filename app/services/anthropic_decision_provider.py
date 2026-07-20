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

from app.config import ANTHROPIC_MAX_RETRIES, ANTHROPIC_MODEL, ANTHROPIC_TIMEOUT_SECONDS
from app.services import tool_schemas
from app.services.tool_decision import ToolDecision
from app.services.tool_registry import AVAILABLE_TOOLS

_SYSTEM_PROMPT = (
    "You are the decision-making layer of a small task-management assistant. "
    "Given the user's message, decide which single tool (if any) should handle "
    "it, and call that tool with appropriate arguments. If no tool fits the "
    "message, don't call any tool."
)


class AnthropicDecisionError(Exception):
    """Raised whenever the Anthropic provider can't produce a trustworthy decision.

    Covers API/network failures, missing tool calls, unknown tools, and
    invalid arguments - agent_decision.decide_tool catches this one type
    and falls back to the rule-based provider.
    """


def _build_claude_tools() -> list[dict]:
    """Build Claude's native tool definitions from the existing tool registry.

    Only tools with an argument schema are included, which limits Claude
    to exactly the 6 executable tools (e.g. get_task is excluded - it
    has no schema, since this agent doesn't execute it).
    """
    tools = []
    for tool in AVAILABLE_TOOLS:
        if tool.name not in tool_schemas.TOOL_ARGUMENT_SCHEMAS:
            continue
        tools.append(
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool_schemas.TOOL_ARGUMENT_SCHEMAS[tool.name],
            }
        )
    return tools


def _get_client() -> anthropic.Anthropic:
    """Construct the Anthropic client. Kept as its own function so tests
    can monkeypatch it instead of making a real API call.

    max_retries makes the SDK's own bounded retry (connection errors,
    408/409/429/5xx only - never a 4xx) explicit/configurable instead of
    relying on its invisible default (which is also 2) - see
    app.config.ANTHROPIC_MAX_RETRIES.
    """
    return anthropic.Anthropic(timeout=ANTHROPIC_TIMEOUT_SECONDS, max_retries=ANTHROPIC_MAX_RETRIES)


def _parse_tool_use(block) -> ToolDecision:
    tool_name = getattr(block, "name", None)
    arguments = getattr(block, "input", None)

    try:
        tool_schemas.validate_tool_call(tool_name, arguments)
    except tool_schemas.ToolCallValidationError as exc:
        raise AnthropicDecisionError(str(exc)) from exc

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

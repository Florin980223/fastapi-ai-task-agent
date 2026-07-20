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

import logging
import time

import anthropic

from app.config import ANTHROPIC_MAX_RETRIES, ANTHROPIC_MODEL, ANTHROPIC_TIMEOUT_SECONDS
from app.services import decision_validation, tool_schemas
from app.services.tool_decision import ToolDecision
from app.services.tool_registry import AVAILABLE_TOOLS

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are the decision-making layer of a small task-management assistant. "
    "Given the user's message, decide which single tool (if any) should handle "
    "it, and call that tool with appropriate arguments. If no tool fits the "
    "message, don't call any tool. Respond only through the provided tool-call "
    "mechanism - never with plain prose."
)


class AnthropicDecisionError(decision_validation.DecisionProviderError):
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


def _parse_tool_use(block) -> tuple[str | None, dict]:
    """Extract and validate a tool name/arguments from a tool_use content
    block. Raises tool_schemas.ToolCallValidationError on an invalid call -
    caught by decision_validation.attempt_with_repair's caller below.
    """
    tool_name = getattr(block, "name", None)
    arguments = getattr(block, "input", None)
    tool_schemas.validate_tool_call(tool_name, arguments)
    return tool_name, arguments


def _find_tool_use_block(response):
    return next((block for block in response.content if getattr(block, "type", None) == "tool_use"), None)


def _log_decision_outcome(started: float, *, repaired: bool, outcome: str, failure_category: str | None) -> None:
    """Structured log line for one decision call. Fields only - never the
    user's message, the model's raw response, or any argument value.
    """
    logger.info(
        "anthropic decision provider call",
        extra={
            "provider": "anthropic",
            "model": ANTHROPIC_MODEL,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "repaired": repaired,
            "outcome": outcome,
            "validation_failure_category": failure_category,
        },
    )


def decide_tool(message: str) -> ToolDecision:
    """Ask Claude to pick a tool (and arguments) for the given message.

    On a malformed/invalid first response, makes exactly one constrained
    repair attempt (sending Claude a tool_result marked as an error,
    describing what was wrong) before giving up - never more than one, and
    never for a network/timeout failure, which goes straight to the
    caller's fallback-safety gate instead. Raises AnthropicDecisionError if
    the request fails, Claude doesn't call a tool, or the tool call is
    still invalid after the repair attempt - the caller
    (agent_decision.decide_tool) decides what to do next, it is never
    assumed safe to just fall back to rule_based here.
    """
    started = time.monotonic()
    client = _get_client()
    tools = _build_claude_tools()

    try:
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            tools=tools,
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            messages=[{"role": "user", "content": message}],
        )
    except Exception as exc:  # broad on purpose: a network/timeout failure never gets a repair attempt
        category = decision_validation.classify_validation_failure(exc)
        _log_decision_outcome(started, repaired=False, outcome="failed", failure_category=category)
        raise AnthropicDecisionError(f"Anthropic API request failed: {exc}", category=category) from exc

    tool_use_block = _find_tool_use_block(response)
    if tool_use_block is None:
        # Not treated as repairable: Claude declining to call any tool may
        # simply be correctly reporting "nothing here fits" - see the
        # matching comment in ollama_decision_provider.decide_tool.
        _log_decision_outcome(started, repaired=False, outcome="failed", failure_category="empty_response")
        raise AnthropicDecisionError("Claude did not call a tool for this message.", category="empty_response")

    repair_attempted = []  # mutable cell _repair sets to non-empty as soon as it's called, success or not

    def _repair(exc: Exception) -> tuple[str | None, dict]:
        repair_attempted.append(True)
        try:
            repair_response = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=512,
                system=_SYSTEM_PROMPT,
                tools=tools,
                tool_choice={"type": "auto", "disable_parallel_tool_use": True},
                messages=[
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": [tool_use_block]},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use_block.id,
                                "content": f"That tool call was invalid: {exc}. Call exactly one valid tool with corrected arguments.",
                                "is_error": True,
                            }
                        ],
                    },
                ],
            )
        except Exception as repair_exc:
            category = decision_validation.classify_validation_failure(repair_exc)
            raise AnthropicDecisionError(f"Anthropic repair request failed: {repair_exc}", category=category) from repair_exc

        repair_tool_use_block = _find_tool_use_block(repair_response)
        if repair_tool_use_block is None:
            raise AnthropicDecisionError("Claude did not call a tool for the repair attempt.", category="empty_response")
        return _parse_tool_use(repair_tool_use_block)

    try:
        tool_name, arguments = decision_validation.attempt_with_repair(
            primary=lambda: _parse_tool_use(tool_use_block),
            repair=_repair,
        )
    except Exception as exc:
        category = decision_validation.classify_validation_failure(exc)
        _log_decision_outcome(started, repaired=bool(repair_attempted), outcome="failed", failure_category=category)
        if isinstance(exc, AnthropicDecisionError):
            raise
        raise AnthropicDecisionError(str(exc), category=category) from exc

    repaired = bool(repair_attempted)
    _log_decision_outcome(
        started,
        repaired=repaired,
        outcome="executed_after_repair" if repaired else "executed_first_try",
        failure_category=None,
    )
    return ToolDecision(
        selected_tool=tool_name,
        arguments=arguments,
        reason=f"Claude (model {ANTHROPIC_MODEL}) selected the '{tool_name}' tool for this message.",
    )

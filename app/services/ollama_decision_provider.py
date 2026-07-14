"""A local-Ollama-powered "decision provider" for the agent.

Like the Anthropic provider, this only decides WHICH tool to use and
WHAT arguments to call it with - it returns a ToolDecision and never
runs anything itself. Ollama is only ever asked to select one of the
app's own tools via its native (OpenAI-compatible) tool calling; it
cannot run arbitrary code, shell commands, or HTTP requests. The
caller (agent_decision.decide_tool) is responsible for falling back to
the rule-based provider if anything here fails.
"""

import json

import httpx

from app.config import OLLAMA_BASE_URL, OLLAMA_MODEL
from app.services import tool_schemas
from app.services.tool_decision import ToolDecision
from app.services.tool_registry import AVAILABLE_TOOLS

_SYSTEM_PROMPT = (
    "You are the decision-making layer of a small task-management assistant. "
    "Given the user's message, decide which single tool (if any) should handle "
    "it, and call that tool with appropriate arguments. If no tool fits the "
    "message, don't call any tool."
)

# Local Ollama can be slow to respond on a cold model load, so this is more
# generous than the Anthropic provider's timeout.
_REQUEST_TIMEOUT_SECONDS = 30.0


class OllamaDecisionError(Exception):
    """Raised whenever the Ollama provider can't produce a trustworthy decision.

    Covers the server being unreachable/slow, invalid JSON, missing tool
    calls, unknown tools, and invalid arguments - agent_decision.decide_tool
    catches this one type and falls back to the rule-based provider.
    """


def _build_ollama_tools() -> list[dict]:
    """Build Ollama's native (OpenAI-style) tool definitions from the
    existing tool registry.

    Only tools with an argument schema are included, which limits Ollama
    to exactly the 6 executable tools (e.g. get_task is excluded - it
    has no schema, since this agent doesn't execute it).
    """
    tools = []
    for tool in AVAILABLE_TOOLS:
        if tool.name not in tool_schemas.TOOL_ARGUMENT_SCHEMAS:
            continue
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool_schemas.TOOL_ARGUMENT_SCHEMAS[tool.name],
                },
            }
        )
    return tools


def _call_ollama(payload: dict) -> dict:
    """POST to Ollama's /api/chat and return the parsed JSON response.

    Kept as its own function so tests can monkeypatch it instead of
    making a real HTTP call.
    """
    response = httpx.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload, timeout=_REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def decide_tool(message: str) -> ToolDecision:
    """Ask a local Ollama model to pick a tool (and arguments) for the message.

    Raises OllamaDecisionError if the request fails, times out, returns
    invalid JSON, doesn't call a tool, or the tool call is invalid - the
    caller is expected to fall back to the rule-based provider in that case.
    """
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ],
        "tools": _build_ollama_tools(),
        "stream": False,
        "think": False,
    }

    try:
        data = _call_ollama(payload)
    except Exception as exc:  # broad on purpose: any failure here must trigger a safe fallback
        raise OllamaDecisionError(f"Ollama request failed: {exc}") from exc

    tool_calls = (data.get("message") or {}).get("tool_calls") or []
    if not tool_calls:
        raise OllamaDecisionError("Ollama did not call a tool for this message.")

    function = tool_calls[0].get("function") or {}
    tool_name = function.get("name")
    arguments = function.get("arguments")

    # Ollama's arguments are usually already a JSON object, but some
    # models return them as a JSON string - handle that case too.
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise OllamaDecisionError(f"Ollama returned invalid JSON arguments: {exc}") from exc

    try:
        tool_schemas.validate_tool_call(tool_name, arguments)
    except tool_schemas.ToolCallValidationError as exc:
        raise OllamaDecisionError(str(exc)) from exc

    return ToolDecision(
        selected_tool=tool_name,
        arguments=arguments,
        reason=f"Ollama (model {OLLAMA_MODEL}) selected the '{tool_name}' tool for this message.",
    )

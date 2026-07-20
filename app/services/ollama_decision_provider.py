"""A local-Ollama-powered "decision provider" for the agent.

Like the Anthropic provider, this only decides WHICH tool to use and
WHAT arguments to call it with - it returns a ToolDecision and never
runs anything itself. Ollama is only ever asked to select one of the
app's own tools via its native (OpenAI-compatible) tool calling; it
cannot run arbitrary code, shell commands, or HTTP requests. The
caller (agent_decision.decide_tool) is responsible for falling back to
the rule-based provider if anything here fails.
"""

import copy
import json

import httpx

from app.config import OLLAMA_BASE_URL, OLLAMA_MODEL, OLLAMA_TIMEOUT_SECONDS
from app.services import tool_schemas
from app.services.tool_decision import ToolDecision
from app.services.tool_registry import AVAILABLE_TOOLS

_SYSTEM_PROMPT = (
    "You are the decision-making layer of a small task-management assistant.\n\n"
    "Given the user's message, select the ONE tool that matches the user's "
    "final requested action - not an intermediate or preparatory step.\n\n"
    "Rules:\n"
    "- Always select the tool for what the user ultimately wants to happen, "
    "even if you don't have all the information needed to call it yet. "
    "Missing required arguments (like a task ID) are allowed and expected - "
    "do not avoid selecting a tool just because an argument is missing.\n"
    "- Do not select list_tasks merely because you don't know a task's ID. "
    "list_tasks is only for when the user explicitly wants to view, show, "
    "list, or filter their tasks - never as a way to \"look up\" an ID for "
    "another action.\n"
    "- Select delete_task when the user wants to delete or remove a task, "
    "even if they don't specify which one.\n"
    "- Select update_task when the user wants to rename, edit, or otherwise "
    "modify a task, even if the task ID or new title is missing.\n"
    "- Select mark_task_done when the user says a task is finished, done, or "
    "completed, even if they don't specify which one.\n"
    "- If no tool matches the user's message at all, don't call any tool.\n\n"
    "Examples:\n"
    '- "I want to delete one of my tasks, but I do not know its ID" -> call delete_task with no arguments.\n'
    '- "I finished one of my tasks" -> call mark_task_done with no arguments.\n'
    '- "Show me my tasks" -> call list_tasks.\n'
    '- "Rename one of my tasks" -> call update_task with no arguments.'
)

# Local Ollama can be slow to respond on a cold model load - see
# app.config.OLLAMA_TIMEOUT_SECONDS for the actual (generous, more so
# than the Anthropic provider's) timeout value used below.


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

    Each tool gets its own copy of the canonical schema with the JSON
    Schema "required" field dropped: a JSON Schema saying an argument is
    mandatory can make a local model avoid the tool entirely, or invent
    a value, when it's missing that argument - which directly conflicts
    with the system prompt's instruction that missing arguments are
    allowed. What's actually required is still defined by
    tool_schemas.REQUIRED_ARGUMENTS and enforced in Python afterwards
    (validate_tool_call for types, clarification.py for presence) -
    never by constraining the model's own schema. The canonical schema
    in tool_schemas.py (used by the Anthropic provider) is untouched;
    properties are deep-copied here so nothing this function does can
    ever mutate it.
    """
    tools = []
    for tool in AVAILABLE_TOOLS:
        if tool.name not in tool_schemas.TOOL_ARGUMENT_SCHEMAS:
            continue
        canonical_schema = tool_schemas.TOOL_ARGUMENT_SCHEMAS[tool.name]
        ollama_schema = {
            "type": canonical_schema["type"],
            "properties": copy.deepcopy(canonical_schema["properties"]),
        }
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": ollama_schema,
                },
            }
        )
    return tools


def _call_ollama(payload: dict) -> dict:
    """POST to Ollama's /api/chat and return the parsed JSON response.

    Kept as its own function so tests can monkeypatch it instead of
    making a real HTTP call.
    """
    response = httpx.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload, timeout=OLLAMA_TIMEOUT_SECONDS)
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
        "options": {"temperature": 0},
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

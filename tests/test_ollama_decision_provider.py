"""Tests for the Ollama decision provider and its fallback wiring.

These never call a real Ollama server: ollama_decision_provider._call_ollama
is monkeypatched to return a small fake response (or raise) instead.
"""

import httpx
import pytest

import app.services.agent_decision as agent_decision
import app.services.ollama_decision_provider as ollama_decision_provider
import app.services.tool_schemas as tool_schemas


def _fake_chat_response(tool_calls=None, content=""):
    return {
        "model": "qwen3:4b",
        "message": {
            "role": "assistant",
            "content": content,
            **({"tool_calls": tool_calls} if tool_calls is not None else {}),
        },
        "done": True,
    }


def _tool_call(name, arguments):
    return {"function": {"name": name, "arguments": arguments}}


def test_valid_tool_selection(monkeypatch):
    response = _fake_chat_response([_tool_call("mark_task_done", {"task_id": 3})])
    monkeypatch.setattr(ollama_decision_provider, "_call_ollama", lambda payload: response)

    decision = ollama_decision_provider.decide_tool("Mark task 3 as done")

    assert decision.selected_tool == "mark_task_done"
    assert "Ollama" in decision.reason


def test_correct_argument_parsing(monkeypatch):
    response = _fake_chat_response([_tool_call("update_task", {"task_id": 2, "title": "Buy oat milk"})])
    monkeypatch.setattr(ollama_decision_provider, "_call_ollama", lambda payload: response)

    decision = ollama_decision_provider.decide_tool("Update task 2 to Buy oat milk")

    assert decision.selected_tool == "update_task"
    assert decision.arguments == {"task_id": 2, "title": "Buy oat milk"}


def test_unknown_tool_falls_back_to_rule_based(monkeypatch):
    response = _fake_chat_response([_tool_call("delete_everything", {"task_id": 1})])
    monkeypatch.setattr(agent_decision, "DECISION_PROVIDER", "ollama")
    monkeypatch.setattr(ollama_decision_provider, "_call_ollama", lambda payload: response)

    decision = agent_decision.decide_tool("Delete task 1")

    assert decision.selected_tool == "delete_task"
    assert decision.arguments == {"task_id": 1}


def test_missing_tool_call_falls_back_to_rule_based(monkeypatch):
    response = _fake_chat_response(tool_calls=None, content="I'm not sure what to do.")
    monkeypatch.setattr(agent_decision, "DECISION_PROVIDER", "ollama")
    monkeypatch.setattr(ollama_decision_provider, "_call_ollama", lambda payload: response)

    decision = agent_decision.decide_tool("Delete task 1")

    assert decision.selected_tool == "delete_task"
    assert decision.arguments == {"task_id": 1}


def test_missing_required_argument_does_not_fall_back(monkeypatch):
    # delete_task requires task_id, which is missing here. This is NOT a
    # provider failure - Ollama correctly picked the tool, it just didn't
    # have enough info. The decision should be preserved as-is (for the
    # clarification layer to handle), not discarded via a fallback.
    response = _fake_chat_response([_tool_call("delete_task", {})])
    monkeypatch.setattr(ollama_decision_provider, "_call_ollama", lambda payload: response)

    decision = ollama_decision_provider.decide_tool("Delete a task")

    assert decision.selected_tool == "delete_task"
    assert decision.arguments == {}
    assert "Ollama" in decision.reason


def test_wrong_argument_type_falls_back_to_rule_based(monkeypatch):
    # task_id is present but the wrong type - this IS a provider failure.
    response = _fake_chat_response([_tool_call("delete_task", {"task_id": "not-a-number"})])
    monkeypatch.setattr(agent_decision, "DECISION_PROVIDER", "ollama")
    monkeypatch.setattr(ollama_decision_provider, "_call_ollama", lambda payload: response)

    decision = agent_decision.decide_tool("Delete task 1")

    assert decision.selected_tool == "delete_task"
    assert decision.arguments == {"task_id": 1}


@pytest.mark.parametrize(
    "exception",
    [
        httpx.ConnectError("connection refused"),
        httpx.TimeoutException("request timed out"),
    ],
)
def test_connection_or_timeout_falls_back_to_rule_based(monkeypatch, exception):
    def raise_exception(payload):
        raise exception

    monkeypatch.setattr(agent_decision, "DECISION_PROVIDER", "ollama")
    monkeypatch.setattr(ollama_decision_provider, "_call_ollama", raise_exception)

    decision = agent_decision.decide_tool("Delete task 1")

    assert decision.selected_tool == "delete_task"
    assert decision.arguments == {"task_id": 1}


def _capture_payload(monkeypatch, response):
    """Monkeypatch _call_ollama to record the payload it was called with."""
    captured = {}

    def fake_call_ollama(payload):
        captured["payload"] = payload
        return response

    monkeypatch.setattr(ollama_decision_provider, "_call_ollama", fake_call_ollama)
    return captured


def test_request_includes_improved_routing_system_prompt(monkeypatch):
    response = _fake_chat_response([_tool_call("mark_task_done", {"task_id": 1})])
    captured = _capture_payload(monkeypatch, response)

    ollama_decision_provider.decide_tool("Mark task 1 as done")

    system_message = captured["payload"]["messages"][0]
    assert system_message["role"] == "system"
    content = system_message["content"]
    assert "final requested action" in content
    assert "Do not select list_tasks merely because" in content
    assert "I want to delete one of my tasks, but I do not know its ID" in content


def test_request_sets_temperature_to_zero(monkeypatch):
    response = _fake_chat_response([_tool_call("mark_task_done", {"task_id": 1})])
    captured = _capture_payload(monkeypatch, response)

    ollama_decision_provider.decide_tool("Mark task 1 as done")

    assert captured["payload"]["options"] == {"temperature": 0}


def test_ollama_delete_task_schema_has_no_required_field():
    tools = ollama_decision_provider._build_ollama_tools()
    delete_task_tool = next(tool for tool in tools if tool["function"]["name"] == "delete_task")
    parameters = delete_task_tool["function"]["parameters"]

    # No "required" field (or an empty one) - an empty arguments object
    # must be structurally valid, so the model isn't discouraged from
    # selecting delete_task just because it doesn't know the task id.
    assert "required" not in parameters or parameters["required"] == []


def test_ollama_delete_task_schema_keeps_task_id_type():
    tools = ollama_decision_provider._build_ollama_tools()
    delete_task_tool = next(tool for tool in tools if tool["function"]["name"] == "delete_task")
    parameters = delete_task_tool["function"]["parameters"]

    assert parameters["properties"]["task_id"]["type"] == "integer"


def test_canonical_schema_still_marks_task_id_as_required():
    # The shared, canonical schema (used by Anthropic and as the source
    # of truth for clarification) must be untouched by the Ollama-only
    # schema adjustment.
    assert tool_schemas.TOOL_ARGUMENT_SCHEMAS["delete_task"]["required"] == ["task_id"]

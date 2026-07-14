"""Tests for the Ollama decision provider and its fallback wiring.

These never call a real Ollama server: ollama_decision_provider._call_ollama
is monkeypatched to return a small fake response (or raise) instead.
"""

import httpx
import pytest

import app.services.agent_decision as agent_decision
import app.services.ollama_decision_provider as ollama_decision_provider


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


def test_invalid_arguments_falls_back_to_rule_based(monkeypatch):
    # delete_task requires task_id, which is missing here.
    response = _fake_chat_response([_tool_call("delete_task", {})])
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

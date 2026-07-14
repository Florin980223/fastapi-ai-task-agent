"""Tests for the Anthropic decision provider and its fallback wiring.

These never call the real Anthropic API: anthropic_decision_provider._get_client
is monkeypatched to return a small fake client instead.
"""

from types import SimpleNamespace

import app.config as config
import app.services.agent_decision as agent_decision
import app.services.anthropic_decision_provider as anthropic_decision_provider


class _FakeMessages:
    def __init__(self, response=None, exception=None):
        self._response = response
        self._exception = exception

    def create(self, **kwargs):
        if self._exception is not None:
            raise self._exception
        return self._response


class _FakeClient:
    def __init__(self, response=None, exception=None):
        self.messages = _FakeMessages(response=response, exception=exception)


def _fake_response(blocks):
    return SimpleNamespace(content=blocks)


def _tool_use_block(name, input_):
    return SimpleNamespace(type="tool_use", id="toolu_1", name=name, input=input_)


def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def test_valid_tool_selection(monkeypatch):
    response = _fake_response([_tool_use_block("mark_task_done", {"task_id": 3})])
    monkeypatch.setattr(anthropic_decision_provider, "_get_client", lambda: _FakeClient(response=response))

    decision = anthropic_decision_provider.decide_tool("Mark task 3 as done")

    assert decision.selected_tool == "mark_task_done"
    assert "Claude" in decision.reason


def test_tool_arguments_parsing(monkeypatch):
    response = _fake_response([_tool_use_block("update_task", {"task_id": 2, "title": "Buy oat milk"})])
    monkeypatch.setattr(anthropic_decision_provider, "_get_client", lambda: _FakeClient(response=response))

    decision = anthropic_decision_provider.decide_tool("Update task 2 to Buy oat milk")

    assert decision.selected_tool == "update_task"
    assert decision.arguments == {"task_id": 2, "title": "Buy oat milk"}


def test_unknown_tool_falls_back_to_rule_based(monkeypatch):
    response = _fake_response([_tool_use_block("delete_everything", {"task_id": 1})])
    monkeypatch.setattr(config, "DECISION_PROVIDER", "anthropic")
    monkeypatch.setattr(agent_decision, "DECISION_PROVIDER", "anthropic")
    monkeypatch.setattr(anthropic_decision_provider, "_get_client", lambda: _FakeClient(response=response))

    decision = agent_decision.decide_tool("Delete task 1")

    assert decision.selected_tool == "delete_task"
    assert decision.arguments == {"task_id": 1}


def test_missing_tool_call_falls_back_to_rule_based(monkeypatch):
    response = _fake_response([_text_block("I'm not sure what to do.")])
    monkeypatch.setattr(agent_decision, "DECISION_PROVIDER", "anthropic")
    monkeypatch.setattr(anthropic_decision_provider, "_get_client", lambda: _FakeClient(response=response))

    decision = agent_decision.decide_tool("Delete task 1")

    assert decision.selected_tool == "delete_task"
    assert decision.arguments == {"task_id": 1}


def test_api_exception_falls_back_to_rule_based(monkeypatch):
    monkeypatch.setattr(agent_decision, "DECISION_PROVIDER", "anthropic")
    monkeypatch.setattr(
        anthropic_decision_provider,
        "_get_client",
        lambda: _FakeClient(exception=RuntimeError("network is down")),
    )

    decision = agent_decision.decide_tool("Delete task 1")

    assert decision.selected_tool == "delete_task"
    assert decision.arguments == {"task_id": 1}


def test_rule_based_is_the_default_provider():
    assert config.DECISION_PROVIDER == "rule_based"

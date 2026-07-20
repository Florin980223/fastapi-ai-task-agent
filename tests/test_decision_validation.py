"""Tests for the shared LLM-provider validation/repair helpers
(app/services/decision_validation.py), the unknown-argument rejection
added to app/services/tool_schemas.validate_tool_call, and the sentinel-
value proof that no structured log line emitted by any of this new code
ever contains user message text or tool argument values.

Most of this file is pure unit tests of the shared layer itself.
Provider-specific repair-retry *behavior* is tested in each provider's own
test file - only the sentinel log-scrubbing tests near the bottom touch
Ollama's mocked seams, and only to capture log records, never to assert
anything about decision correctness (that's covered elsewhere).
"""

import json
import logging

import pytest
from pydantic import ValidationError

from app.services import agent_decision, agent_planner, clarification, decision_validation, tool_schemas
from app.services.tool_schemas import ToolCallValidationError


# --- DESTRUCTIVE_TOOLS: single source of truth --------------------------


def test_clarification_destructive_tools_matches_tool_schemas():
    assert clarification._DESTRUCTIVE_TOOLS is tool_schemas.DESTRUCTIVE_TOOLS


def test_delete_task_is_never_in_the_planner_allowlist():
    assert "delete_task" not in agent_planner._ALLOWED_PLANNER_TOOLS
    assert agent_planner._ALLOWED_PLANNER_TOOLS == set(tool_schemas.REQUIRED_ARGUMENTS) - tool_schemas.DESTRUCTIVE_TOOLS


# --- validate_tool_call: unknown-argument rejection ---------------------


def test_unknown_argument_is_rejected():
    with pytest.raises(ToolCallValidationError, match="unsupported argument"):
        tool_schemas.validate_tool_call("create_task", {"title": "Buy milk", "priority": "high"})


def test_known_arguments_still_pass():
    tool_schemas.validate_tool_call("create_task", {"title": "Buy milk"})
    tool_schemas.validate_tool_call("list_tasks", {"done": True})
    tool_schemas.validate_tool_call("list_tasks", {})


def test_unknown_argument_on_a_tool_with_no_arguments_is_rejected():
    with pytest.raises(ToolCallValidationError, match="unsupported argument"):
        tool_schemas.validate_tool_call("list_tasks", {"filter": "done"})


# --- attempt_with_repair -------------------------------------------------


def test_primary_success_never_calls_repair():
    repair_calls = []

    result = decision_validation.attempt_with_repair(
        primary=lambda: "ok",
        repair=lambda exc: repair_calls.append(exc) or "should not happen",
    )

    assert result == "ok"
    assert repair_calls == []


@pytest.mark.parametrize(
    "make_primary_error",
    [
        lambda: ToolCallValidationError("bad tool call"),
        lambda: json.JSONDecodeError("bad json", "{}", 0),
        lambda: ValidationError.from_exception_data("AgentPlan", []),
    ],
)
def test_repairable_failure_triggers_exactly_one_repair_call(make_primary_error):
    call_count = {"repair": 0}

    def primary():
        raise make_primary_error()

    def repair(exc):
        call_count["repair"] += 1
        return "repaired"

    result = decision_validation.attempt_with_repair(primary, repair)

    assert result == "repaired"
    assert call_count["repair"] == 1


def test_repair_failure_propagates_and_is_not_retried_again():
    call_count = {"primary": 0, "repair": 0}

    def primary():
        call_count["primary"] += 1
        raise ToolCallValidationError("first failure")

    def repair(exc):
        call_count["repair"] += 1
        raise ToolCallValidationError("repair also failed")

    with pytest.raises(ToolCallValidationError, match="repair also failed"):
        decision_validation.attempt_with_repair(primary, repair)

    assert call_count == {"primary": 1, "repair": 1}


def test_network_style_failure_is_never_repaired():
    """A connection/timeout-shaped exception must propagate straight
    through - never caught, never given a repair attempt, so it reaches
    the caller's own fallback-safety gate exactly like it did before this
    helper existed.
    """
    repair_calls = []

    def primary():
        raise ConnectionError("could not reach the server")

    with pytest.raises(ConnectionError, match="could not reach the server"):
        decision_validation.attempt_with_repair(primary, lambda exc: repair_calls.append(exc))

    assert repair_calls == []


# --- classify_validation_failure ----------------------------------------


def test_classify_malformed_json():
    exc = json.JSONDecodeError("bad json", "{}", 0)
    assert decision_validation.classify_validation_failure(exc) == "malformed_json"


def test_classify_invalid_plan_shape():
    exc = ValidationError.from_exception_data("AgentPlan", [])
    assert decision_validation.classify_validation_failure(exc) == "invalid_plan_shape"


def test_classify_unknown_tool():
    exc = ToolCallValidationError("Model selected an unknown tool: 'nonexistent_tool'.")
    assert decision_validation.classify_validation_failure(exc) == "unknown_tool"


def test_classify_unknown_argument():
    exc = ToolCallValidationError("Model's call to 'create_task' has an unsupported argument: 'priority'.")
    assert decision_validation.classify_validation_failure(exc) == "unknown_argument"


def test_classify_wrong_type():
    exc = ToolCallValidationError("Model's call to 'create_task' has the wrong type for 'title'.")
    assert decision_validation.classify_validation_failure(exc) == "wrong_type"


def test_classify_timeout():
    class FakeTimeoutException(Exception):
        pass

    assert decision_validation.classify_validation_failure(FakeTimeoutException("timed out")) == "timeout"


def test_classify_generic_network_error():
    assert decision_validation.classify_validation_failure(ConnectionError("refused")) == "network_error"


def test_classify_never_echoes_exception_message():
    """The category string must never contain any fragment of the
    exception's own message - only a fixed category name - since that
    message could echo back a piece of the model's raw output.
    """
    secret_looking_message = "Model's call to 'create_task' has the wrong type for 'title': SENTINEL-XYZ"
    category = decision_validation.classify_validation_failure(ToolCallValidationError(secret_looking_message))
    assert "SENTINEL-XYZ" not in category
    assert category == "wrong_type"


# --- Sentinel-value proof: no user content ever reaches a log line -----


def _assert_sentinel_absent_from_every_log_record(caplog, *sentinels: str) -> None:
    for record in caplog.records:
        haystacks = [record.getMessage(), *[str(value) for value in record.__dict__.values()]]
        for sentinel in sentinels:
            for haystack in haystacks:
                assert sentinel not in haystack, f"sentinel {sentinel!r} leaked into log record: {haystack!r}"


def test_no_sentinel_value_ever_appears_in_ollama_decision_provider_logs(monkeypatch, caplog):
    from app.services import ollama_decision_provider

    sentinel_message = "SENTINEL-MSG-7f3a create a task"
    sentinel_argument = "SENTINEL-ARG-91be"

    call_count = {"n": 0}

    def fake_call_ollama(payload):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First attempt: unknown tool - malformed, triggers repair.
            tool_name = "nonexistent_tool"
        else:
            # Repair attempt: a real tool, still carrying the sentinel
            # argument value - proves even a *successful* decision's log
            # line never echoes an argument value.
            tool_name = "create_task"
        return {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": tool_name, "arguments": {"title": sentinel_argument}}}],
            },
            "done": True,
        }

    monkeypatch.setattr(agent_decision, "DECISION_PROVIDER", "ollama")
    monkeypatch.setattr(ollama_decision_provider, "_call_ollama", fake_call_ollama)

    with caplog.at_level(logging.INFO):
        decision = agent_decision.decide_tool(sentinel_message)

    # Sanity: the decision itself DOES carry the real value (the app needs
    # it to function) - only the logs must never contain it.
    assert decision.selected_tool == "create_task"
    assert decision.arguments == {"title": sentinel_argument}

    _assert_sentinel_absent_from_every_log_record(caplog, sentinel_message, sentinel_argument)


def test_no_sentinel_value_ever_appears_in_ollama_decision_provider_logs_on_total_failure(monkeypatch, caplog):
    from app.services import ollama_decision_provider

    sentinel_message = "SENTINEL-MSG-total-failure"

    def raise_network_error(payload):
        raise ConnectionError(f"simulated outage for {sentinel_message}")

    monkeypatch.setattr(agent_decision, "DECISION_PROVIDER", "ollama")
    monkeypatch.setattr(ollama_decision_provider, "_call_ollama", raise_network_error)

    with caplog.at_level(logging.INFO):
        # This particular sentinel message is single-step/non-destructive,
        # so it safely falls back to rule_based rather than raising - the
        # point of this test is only what ends up in the logs, not which
        # outcome occurs.
        agent_decision.decide_tool(sentinel_message)

    _assert_sentinel_absent_from_every_log_record(caplog, sentinel_message)


def test_no_sentinel_value_ever_appears_in_planning_logs(monkeypatch, caplog):
    from app.services import ollama_planner_provider

    sentinel_title = "SENTINEL-TITLE-abcd"
    sentinel_message = "SENTINEL-MSG-plan create a task then list tasks"

    response = {
        "message": {
            "role": "assistant",
            "content": json.dumps(
                {
                    "steps": [
                        {"tool": "create_task", "arguments": {"title": sentinel_title}},
                        {"tool": "list_tasks", "arguments": {}},
                    ]
                }
            ),
        },
        "done": True,
    }
    monkeypatch.setattr(ollama_planner_provider, "_call_ollama_plan", lambda payload: response)

    with caplog.at_level(logging.INFO):
        plan = agent_planner.decide_plan(sentinel_message)

    assert plan is not None
    assert plan.steps[0].arguments["title"] == sentinel_title  # sanity: the plan itself carries the real value

    _assert_sentinel_absent_from_every_log_record(caplog, sentinel_title, sentinel_message)

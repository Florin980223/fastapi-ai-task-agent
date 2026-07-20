"""Tests for safe multi-step agent execution on POST /agent/execute.

A message that looks multi-step (contains an explicit English or Romanian
cue) may be broken into an ordered plan of 2-3 existing tools when
AGENT_MULTI_STEP_PLANNING is enabled and the provider is "ollama". These
tests never call a real Ollama server: ollama_planner_provider._call_ollama_plan
is monkeypatched to return a small fake response (or raise) instead, exactly
like existing tests monkeypatch ollama_decision_provider._call_ollama.

Two outcomes are deliberately kept distinct and both tested: a message that
doesn't look multi-step (or planning isn't applicable) always uses the
ordinary, unmodified single-step pipeline; a message that DOES look
multi-step but fails to produce a valid, safe plan executes no tools at all
- it never falls back to guessing a single action from a multi-action
message.
"""

import json
import uuid

import httpx
import pydantic
import pytest

import app.services.agent_decision as agent_decision
import app.services.agent_planner as agent_planner
import app.services.ollama_decision_provider as ollama_decision_provider
import app.services.ollama_planner_provider as ollama_planner_provider
from app.config import AGENT_MAX_PLAN_STEPS
from app.services import weather_service
from app.services.agent_plan import AgentPlan, PlannedStep


def _execute(client, message, conversation_id=None):
    body = {"message": message}
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    response = client.post("/agent/execute", json=body)
    assert response.status_code == 200
    return response.json()


def _fake_plan_chat_response(steps):
    return {
        "model": "qwen3:4b",
        "message": {"role": "assistant", "content": json.dumps({"steps": steps})},
        "done": True,
    }


def _enable_planning(monkeypatch):
    monkeypatch.setattr(agent_decision, "DECISION_PROVIDER", "ollama")
    monkeypatch.setattr(agent_planner, "MULTI_STEP_PLANNING_ENABLED", True)


# --- looks_multi_step -------------------------------------------------


@pytest.mark.parametrize("cue", ["then", "after that"])
def test_english_cue_is_detected(cue):
    assert agent_planner.looks_multi_step(f"Create a task {cue} show me all tasks")


@pytest.mark.parametrize("cue", ["și apoi", "iar apoi", "apoi", "după aceea"])
def test_romanian_cue_is_detected(cue):
    message = f"Creează un task {cue} arată-mi toate task-urile"
    assert agent_planner.looks_multi_step(message)
    assert agent_planner.looks_multi_step(message.upper())


def test_plain_message_is_not_detected_as_multi_step():
    assert agent_planner.looks_multi_step("Add a task to buy milk") is False


# --- _validate_plan -----------------------------------------------------


def _plan(steps):
    return AgentPlan(steps=steps)


def test_unknown_tool_in_plan_is_rejected():
    plan = _plan(
        [
            PlannedStep(tool="create_task", arguments={"title": "buy milk"}),
            PlannedStep(tool="archive_task", arguments={}),
        ]
    )
    assert agent_planner._validate_plan(plan) is None


def test_delete_task_in_plan_is_rejected():
    plan = _plan(
        [
            PlannedStep(tool="create_task", arguments={"title": "buy milk"}),
            PlannedStep(tool="delete_task", arguments={"task_id": 1}),
        ]
    )
    assert agent_planner._validate_plan(plan) is None


def test_task_id_and_reference_together_is_rejected():
    plan = _plan(
        [
            PlannedStep(tool="create_task", arguments={"title": "buy milk"}),
            PlannedStep(tool="mark_task_done", arguments={"task_id": 5}, task_id_from_step=1),
        ]
    )
    assert agent_planner._validate_plan(plan) is None


def test_out_of_range_reference_is_rejected():
    plan = _plan(
        [
            PlannedStep(tool="create_task", arguments={"title": "buy milk"}),
            PlannedStep(tool="mark_task_done", arguments={}, task_id_from_step=5),
        ]
    )
    assert agent_planner._validate_plan(plan) is None


def test_forward_reference_is_rejected():
    plan = _plan(
        [
            PlannedStep(tool="mark_task_done", arguments={}, task_id_from_step=2),
            PlannedStep(tool="create_task", arguments={"title": "buy milk"}),
        ]
    )
    assert agent_planner._validate_plan(plan) is None


def test_self_reference_is_rejected():
    plan = _plan(
        [
            PlannedStep(tool="create_task", arguments={"title": "buy milk"}),
            PlannedStep(tool="mark_task_done", arguments={}, task_id_from_step=2),
        ]
    )
    assert agent_planner._validate_plan(plan) is None


def test_reference_to_wrong_target_tool_is_rejected():
    plan = _plan(
        [
            PlannedStep(tool="list_tasks", arguments={}),
            PlannedStep(tool="mark_task_done", arguments={}, task_id_from_step=1),
        ]
    )
    assert agent_planner._validate_plan(plan) is None


def test_valid_plan_passes_validation():
    plan = _plan(
        [
            PlannedStep(tool="create_task", arguments={"title": "buy milk"}),
            PlannedStep(tool="list_tasks", arguments={}),
        ]
    )
    assert agent_planner._validate_plan(plan) is plan


# --- AgentPlan step-count constraint -------------------------------------


def test_agent_plan_rejects_a_single_step():
    with pytest.raises(pydantic.ValidationError):
        AgentPlan(steps=[PlannedStep(tool="create_task", arguments={"title": "buy milk"})])


def test_agent_plan_rejects_more_than_three_steps():
    with pytest.raises(pydantic.ValidationError):
        AgentPlan(steps=[PlannedStep(tool="list_tasks", arguments={})] * 4)


# --- Execution-layer validation stays authoritative regardless of the
# --- provider - each test below bypasses Pydantic/_validate_plan on
# --- purpose (via model_construct, or by calling execute_plan directly
# --- without ever calling _validate_plan first) to prove the *executor's
# --- own* check is what actually blocks it, not just that something
# --- upstream happened to validate first. ---------------------------


def test_plan_exceeding_the_configured_max_steps_is_rejected_even_bypassing_pydantic():
    # model_construct skips Pydantic validation entirely, so this plan has
    # more steps than AgentPlan's own Field(max_length=...) would ever
    # allow to be constructed normally.
    steps = [PlannedStep(tool="list_tasks", arguments={})] * (AGENT_MAX_PLAN_STEPS + 2)
    bypassed_plan = AgentPlan.model_construct(steps=steps)

    assert agent_planner._validate_plan(bypassed_plan) is None


def test_execute_plan_independently_refuses_delete_task_even_if_validate_plan_is_bypassed(new_db_session, test_user_id):
    # _validate_plan is deliberately never called here - this plan reaches
    # execute_plan exactly as if some future bug let a destructive tool
    # slip past the allowlist check.
    bypassed_plan = AgentPlan.model_construct(
        steps=[
            PlannedStep(tool="delete_task", arguments={"task_id": 1}),
            PlannedStep(tool="list_tasks", arguments={}),
        ]
    )

    results = agent_planner.execute_plan(bypassed_plan, new_db_session, uuid.uuid4(), test_user_id)

    assert len(results) == 1
    assert results[0].status == "stopped"
    assert results[0].result is None
    assert "confirmation" in results[0].error.lower()


def test_execute_plan_rejects_duplicate_destructive_steps(new_db_session, test_user_id):
    bypassed_plan = AgentPlan.model_construct(
        steps=[
            PlannedStep(tool="delete_task", arguments={"task_id": 1}),
            PlannedStep(tool="delete_task", arguments={"task_id": 2}),
        ]
    )

    results = agent_planner.execute_plan(bypassed_plan, new_db_session, uuid.uuid4(), test_user_id)

    # Stops at the first destructive step - the second delete_task step is
    # never even attempted, let alone executed.
    assert len(results) == 1
    assert results[0].status == "stopped"


def test_execute_plan_rejects_an_unsupported_argument(new_db_session, test_user_id):
    # No bypass needed here: PlannedStep.arguments accepts any string key
    # at the Pydantic level - only tool_schemas.validate_tool_call (called
    # inside execute_plan, independently of whatever a provider already
    # validated) catches an argument a tool doesn't actually accept.
    plan = AgentPlan(
        steps=[
            PlannedStep(tool="create_task", arguments={"title": "buy milk", "priority": "high"}),
            PlannedStep(tool="list_tasks", arguments={}),
        ]
    )

    results = agent_planner.execute_plan(plan, new_db_session, uuid.uuid4(), test_user_id)

    assert len(results) == 1
    assert results[0].status == "stopped"
    assert "unsupported argument" in results[0].error.lower()


# --- Route-level integration tests ---------------------------------------


def test_create_task_then_list_tasks_executes_both_steps(client, monkeypatch):
    _enable_planning(monkeypatch)
    response = _fake_plan_chat_response(
        [
            {"tool": "create_task", "arguments": {"title": "buy milk"}},
            {"tool": "list_tasks", "arguments": {}},
        ]
    )
    monkeypatch.setattr(ollama_planner_provider, "_call_ollama_plan", lambda payload: response)

    data = _execute(client, "Create a task to buy milk and then show me all tasks")

    assert data["is_multi_step"] is True
    assert data["selected_tool"] is None
    assert data["needs_clarification"] is False
    assert data["needs_confirmation"] is False
    assert len(data["steps"]) == 2
    assert data["steps"][0]["tool"] == "create_task"
    assert data["steps"][0]["status"] == "success"
    assert data["steps"][0]["result"]["title"] == "buy milk"
    assert data["steps"][1]["tool"] == "list_tasks"
    assert data["steps"][1]["status"] == "success"
    assert len(data["steps"][1]["result"]) == 1


def test_multi_step_plan_created_task_is_owned_by_the_authenticated_user(client, monkeypatch, other_user_headers):
    _enable_planning(monkeypatch)
    response = _fake_plan_chat_response(
        [
            {"tool": "create_task", "arguments": {"title": "buy milk"}},
            {"tool": "list_tasks", "arguments": {}},
        ]
    )
    monkeypatch.setattr(ollama_planner_provider, "_call_ollama_plan", lambda payload: response)

    data = _execute(client, "Create a task to buy milk and then show me all tasks")
    task_id = data["steps"][0]["result"]["id"]

    # The plan's own list_tasks step only ever saw alice's task.
    assert [task["id"] for task in data["steps"][1]["result"]] == [task_id]

    # Bob can neither see it in his own task list nor reach it directly.
    assert client.get("/tasks", headers=other_user_headers).json() == []
    assert client.get(f"/tasks/{task_id}", headers=other_user_headers).status_code == 404


def test_mark_task_done_then_list_tasks(client, monkeypatch):
    created = client.post("/tasks", json={"title": "Buy milk"}).json()
    task_id = created["id"]

    _enable_planning(monkeypatch)
    response = _fake_plan_chat_response(
        [
            {"tool": "mark_task_done", "arguments": {"task_id": task_id}},
            {"tool": "list_tasks", "arguments": {}},
        ]
    )
    monkeypatch.setattr(ollama_planner_provider, "_call_ollama_plan", lambda payload: response)

    data = _execute(client, f"Mark task {task_id} done and then show me all tasks")

    assert data["is_multi_step"] is True
    assert data["steps"][0]["status"] == "success"
    assert data["steps"][0]["result"]["done"] is True
    assert data["steps"][1]["status"] == "success"
    assert data["steps"][1]["result"][0]["done"] is True


def test_create_task_then_mark_task_done_using_step_reference(client, monkeypatch):
    _enable_planning(monkeypatch)
    response = _fake_plan_chat_response(
        [
            {"tool": "create_task", "arguments": {"title": "buy milk"}},
            {"tool": "mark_task_done", "arguments": {}, "task_id_from_step": 1},
        ]
    )
    monkeypatch.setattr(ollama_planner_provider, "_call_ollama_plan", lambda payload: response)

    data = _execute(client, "Create a task to buy milk and then mark it done")

    assert data["is_multi_step"] is True
    created_id = data["steps"][0]["result"]["id"]
    assert data["steps"][1]["arguments"]["task_id"] == created_id
    assert data["steps"][1]["status"] == "success"
    assert data["steps"][1]["result"]["done"] is True


def test_get_weather_then_list_tasks_with_mocked_weather(client, monkeypatch):
    def fake_get_weather_for_city(city):
        return {
            "city": city,
            "country": "United Kingdom",
            "latitude": 51.5,
            "longitude": -0.1,
            "current_temperature": 20.0,
            "wind_speed": 10.0,
            "weather_code": 1,
        }

    monkeypatch.setattr(weather_service, "get_weather_for_city", fake_get_weather_for_city)

    _enable_planning(monkeypatch)
    response = _fake_plan_chat_response(
        [
            {"tool": "get_weather", "arguments": {"city": "London"}},
            {"tool": "list_tasks", "arguments": {}},
        ]
    )
    monkeypatch.setattr(ollama_planner_provider, "_call_ollama_plan", lambda payload: response)

    data = _execute(client, "What's the weather in London and then show me all tasks")

    assert data["is_multi_step"] is True
    assert data["steps"][0]["status"] == "success"
    assert data["steps"][0]["result"]["city"] == "London"
    assert data["steps"][1]["status"] == "success"


def test_missing_arguments_stop_before_execution(client, monkeypatch):
    _enable_planning(monkeypatch)
    response = _fake_plan_chat_response(
        [
            {"tool": "create_task", "arguments": {}},
            {"tool": "list_tasks", "arguments": {}},
        ]
    )
    monkeypatch.setattr(ollama_planner_provider, "_call_ollama_plan", lambda payload: response)

    data = _execute(client, "Create a task and then show me all tasks")

    assert data["is_multi_step"] is True
    assert len(data["steps"]) == 1
    assert data["steps"][0]["status"] == "stopped"
    assert client.get("/tasks").json() == []


def test_failure_in_step_one_prevents_step_two(client, monkeypatch):
    _enable_planning(monkeypatch)
    response = _fake_plan_chat_response(
        [
            {"tool": "mark_task_done", "arguments": {"task_id": 999}},
            {"tool": "list_tasks", "arguments": {}},
        ]
    )
    monkeypatch.setattr(ollama_planner_provider, "_call_ollama_plan", lambda payload: response)

    data = _execute(client, "Mark task 999 done and then show me all tasks")

    assert data["is_multi_step"] is True
    assert len(data["steps"]) == 1
    assert data["steps"][0]["status"] == "error"
    assert data["steps"][0]["result"] == {"error": "Task 999 was not found."}


def test_failure_in_step_two_preserves_step_one_result(client, monkeypatch):
    _enable_planning(monkeypatch)
    response = _fake_plan_chat_response(
        [
            {"tool": "create_task", "arguments": {"title": "buy milk"}},
            {"tool": "mark_task_done", "arguments": {"task_id": 999}},
        ]
    )
    monkeypatch.setattr(ollama_planner_provider, "_call_ollama_plan", lambda payload: response)

    data = _execute(client, "Create a task to buy milk and then mark task 999 done")

    assert data["is_multi_step"] is True
    assert len(data["steps"]) == 2
    assert data["steps"][0]["status"] == "success"
    assert data["steps"][0]["result"]["title"] == "buy milk"
    assert data["steps"][1]["status"] == "error"
    # Step 1's task really was created and persisted, not rolled back.
    assert len(client.get("/tasks").json()) == 1


def test_planner_failure_for_multi_step_message_executes_no_tools(client, monkeypatch):
    _enable_planning(monkeypatch)

    def raise_error(payload):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(ollama_planner_provider, "_call_ollama_plan", raise_error)

    data = _execute(client, "Create a task to buy milk and then show me all tasks")

    assert data["is_multi_step"] is True
    assert data["steps"] == []
    assert data["selected_tool"] is None
    assert data["result"] is None
    assert data["final_answer"] == "I couldn't create a safe multi-step plan. Please rephrase the request."
    assert client.get("/tasks").json() == []


def test_invalid_planner_output_executes_no_tools(client, monkeypatch):
    _enable_planning(monkeypatch)
    response = _fake_plan_chat_response(
        [
            {"tool": "create_task", "arguments": {"title": "buy milk"}},
            {"tool": "archive_task", "arguments": {}},
        ]
    )
    monkeypatch.setattr(ollama_planner_provider, "_call_ollama_plan", lambda payload: response)

    data = _execute(client, "Create a task to buy milk and then archive everything")

    assert data["is_multi_step"] is True
    assert data["steps"] == []
    assert data["final_answer"] == "I couldn't create a safe multi-step plan. Please rephrase the request."
    assert client.get("/tasks").json() == []


def test_delete_task_in_plan_does_not_bypass_confirmation(client, monkeypatch):
    created = client.post("/tasks", json={"title": "Buy milk"}).json()
    task_id = created["id"]

    _enable_planning(monkeypatch)
    response = _fake_plan_chat_response(
        [
            {"tool": "delete_task", "arguments": {"task_id": task_id}},
            {"tool": "list_tasks", "arguments": {}},
        ]
    )
    monkeypatch.setattr(ollama_planner_provider, "_call_ollama_plan", lambda payload: response)

    data = _execute(client, f"Delete task {task_id} and then show me all tasks")

    assert data["is_multi_step"] is True
    assert data["steps"] == []
    assert data["needs_confirmation"] is False
    assert any(task["id"] == task_id for task in client.get("/tasks").json())


def test_normal_single_step_message_never_calls_planner(client, monkeypatch):
    _enable_planning(monkeypatch)

    def fail_if_called(payload):
        raise AssertionError("planner should not be called for a non-multi-step message")

    monkeypatch.setattr(ollama_planner_provider, "_call_ollama_plan", fail_if_called)

    single_response = {
        "model": "qwen3:4b",
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "create_task", "arguments": {"title": "buy milk"}}}],
        },
        "done": True,
    }
    monkeypatch.setattr(ollama_decision_provider, "_call_ollama", lambda payload: single_response)

    data = _execute(client, "Add a task to buy milk")

    assert data["is_multi_step"] is False
    assert data["steps"] == []
    assert data["selected_tool"] == "create_task"
    assert data["result"]["title"] == "buy milk"


def test_romanian_multi_step_cue_invokes_planner(client, monkeypatch):
    _enable_planning(monkeypatch)
    response = _fake_plan_chat_response(
        [
            {"tool": "create_task", "arguments": {"title": "cumpără lapte"}},
            {"tool": "list_tasks", "arguments": {}},
        ]
    )
    monkeypatch.setattr(ollama_planner_provider, "_call_ollama_plan", lambda payload: response)

    data = _execute(client, "Creează un task să cumpăr lapte și apoi arată-mi toate task-urile")

    assert data["is_multi_step"] is True
    assert len(data["steps"]) == 2
    assert data["steps"][0]["status"] == "success"
    assert data["steps"][1]["status"] == "success"


def test_multi_step_flag_has_no_effect_when_provider_is_rule_based(client, monkeypatch):
    monkeypatch.setattr(agent_planner, "MULTI_STEP_PLANNING_ENABLED", True)
    # agent_decision.DECISION_PROVIDER is left untouched (default "rule_based").

    def fail_if_called(payload):
        raise AssertionError("planner should not be called when provider is rule_based")

    monkeypatch.setattr(ollama_planner_provider, "_call_ollama_plan", fail_if_called)

    data = _execute(client, "Create a task to buy milk and then show me all tasks")

    assert data["is_multi_step"] is False
    assert data["selected_tool"] == "create_task"


def test_single_delete_request_still_requires_confirmation_with_planning_enabled(client, monkeypatch):
    created = client.post("/tasks", json={"title": "Buy milk"}).json()
    task_id = created["id"]

    _enable_planning(monkeypatch)

    def fail_if_called(payload):
        raise AssertionError("planner should not be called for a plain delete request")

    monkeypatch.setattr(ollama_planner_provider, "_call_ollama_plan", fail_if_called)

    single_response = {
        "model": "qwen3:4b",
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "delete_task", "arguments": {"task_id": task_id}}}],
        },
        "done": True,
    }
    monkeypatch.setattr(ollama_decision_provider, "_call_ollama", lambda payload: single_response)

    data = _execute(client, f"Delete task {task_id}")

    assert data["is_multi_step"] is False
    assert data["needs_confirmation"] is True
    assert any(task["id"] == task_id for task in client.get("/tasks").json())


# --- _has_destructive_intent (pre-planning safety guard) -----------------


@pytest.mark.parametrize(
    "message",
    [
        "Create a task to test safety and then delete it",
        "Create a task to buy milk and then delete task 3",
        "Create a task and then remove it",
        "Create a task and then remove that task",
        "Create a task and then erase task 2",
    ],
)
def test_english_destructive_phrase_is_detected(message):
    assert agent_planner._has_destructive_intent(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "Creează un task și apoi șterge-l",
        "Creează un task și apoi sterge-l",
        "Creează un task și apoi șterge task-ul",
        "Creează un task și apoi elimină task-ul",
    ],
)
def test_romanian_destructive_phrase_is_detected(message):
    assert agent_planner._has_destructive_intent(message) is True


def test_task_title_containing_delete_is_not_destructive_intent():
    # requirement 11's explicit false-positive example: "delete" appears
    # only inside a task title clause, not as its own destructive clause.
    message = "Create a task called delete old files and then show my tasks"
    assert agent_planner._has_destructive_intent(message) is False


@pytest.mark.parametrize(
    "message",
    [
        "Create a task and then show me all tasks",
        "Create a task and then mark it as done",
    ],
)
def test_safe_multi_step_messages_are_not_destructive_intent(message):
    assert agent_planner._has_destructive_intent(message) is False


# --- Route-level: destructive multi-step intent is blocked ---------------


def test_destructive_delete_it_executes_zero_tools_and_never_calls_planner(client, monkeypatch):
    _enable_planning(monkeypatch)

    def fail_if_called(payload):
        raise AssertionError("Ollama planner must not be called for a destructive multi-step request")

    monkeypatch.setattr(ollama_planner_provider, "_call_ollama_plan", fail_if_called)

    data = _execute(client, "Create a task to test safety and then delete it")

    assert data["is_multi_step"] is True
    assert data["steps"] == []
    assert data["selected_tool"] is None
    assert data["result"] is None
    assert data["final_answer"] == "I couldn't create a safe multi-step plan. Please rephrase the request."
    assert client.get("/tasks").json() == []


def test_destructive_remove_it_is_blocked(client, monkeypatch):
    _enable_planning(monkeypatch)

    def fail_if_called(payload):
        raise AssertionError("Ollama planner must not be called for a destructive multi-step request")

    monkeypatch.setattr(ollama_planner_provider, "_call_ollama_plan", fail_if_called)

    data = _execute(client, "Create a task and then remove it")

    assert data["is_multi_step"] is True
    assert data["steps"] == []
    assert data["final_answer"] == "I couldn't create a safe multi-step plan. Please rephrase the request."
    assert client.get("/tasks").json() == []


def test_destructive_romanian_sterge_l_is_blocked(client, monkeypatch):
    _enable_planning(monkeypatch)

    def fail_if_called(payload):
        raise AssertionError("Ollama planner must not be called for a destructive multi-step request")

    monkeypatch.setattr(ollama_planner_provider, "_call_ollama_plan", fail_if_called)

    data = _execute(client, "Creează un task și apoi șterge-l")

    assert data["is_multi_step"] is True
    assert data["steps"] == []
    assert data["final_answer"] == "I couldn't create a safe multi-step plan. Please rephrase the request."
    assert client.get("/tasks").json() == []


def test_title_containing_delete_word_is_not_blocked(client, monkeypatch):
    _enable_planning(monkeypatch)
    response = _fake_plan_chat_response(
        [
            {"tool": "create_task", "arguments": {"title": "delete old files"}},
            {"tool": "list_tasks", "arguments": {}},
        ]
    )
    monkeypatch.setattr(ollama_planner_provider, "_call_ollama_plan", lambda payload: response)

    data = _execute(client, "Create a task called delete old files and then show my tasks")

    assert data["is_multi_step"] is True
    assert len(data["steps"]) == 2
    assert data["steps"][0]["status"] == "success"
    assert data["steps"][0]["result"]["title"] == "delete old files"
    assert data["steps"][1]["status"] == "success"


def test_safe_multi_step_mark_it_done_not_blocked_by_destructive_guard(client, monkeypatch):
    _enable_planning(monkeypatch)
    response = _fake_plan_chat_response(
        [
            {"tool": "create_task", "arguments": {"title": "buy milk"}},
            {"tool": "mark_task_done", "arguments": {}, "task_id_from_step": 1},
        ]
    )
    monkeypatch.setattr(ollama_planner_provider, "_call_ollama_plan", lambda payload: response)

    data = _execute(client, "Create a task and then mark it as done")

    assert data["is_multi_step"] is True
    assert data["steps"][0]["status"] == "success"
    assert data["steps"][1]["status"] == "success"
    assert data["steps"][1]["result"]["done"] is True

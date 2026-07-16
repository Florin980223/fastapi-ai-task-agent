"""Tests for persistent run tracing on POST /agent/execute.

Every request gets a run_id and a persistent AgentRun (+ AgentRunStep)
trace, queryable via GET /agent/runs and GET /agent/runs/{run_id}. These
tests never call a real Ollama/Anthropic/Open-Meteo API - multi-step tests
reuse the same ollama_planner_provider._call_ollama_plan monkeypatch seam
as tests/test_multi_step_planning.py. All tests run against the isolated
in-memory test database configured in tests/conftest.py, never the
development tasks.db.
"""

import json
import uuid

import app.services.agent_decision as agent_decision
import app.services.agent_planner as agent_planner
import app.services.agent_trace_service as agent_trace_service
import app.services.ollama_planner_provider as ollama_planner_provider


def _execute(client, message, conversation_id=None):
    body = {"message": message}
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    response = client.post("/agent/execute", json=body)
    assert response.status_code == 200
    return response.json()


def _fake_plan_chat_response(steps):
    return {
        "message": {"role": "assistant", "content": json.dumps({"steps": steps})},
        "done": True,
    }


def _enable_planning(monkeypatch):
    monkeypatch.setattr(agent_decision, "DECISION_PROVIDER", "ollama")
    monkeypatch.setattr(agent_planner, "MULTI_STEP_PLANNING_ENABLED", True)


def test_single_step_success_creates_run_and_one_step(client):
    data = _execute(client, "Add a task to buy milk")

    detail = client.get(f"/agent/runs/{data['run_id']}").json()

    assert detail["status"] == "success"
    assert detail["is_multi_step"] is False
    assert detail["selected_tool"] == "create_task"
    assert detail["decision_provider"] == "rule_based"
    assert detail["duration_ms"] >= 0
    assert len(detail["steps"]) == 1

    step = detail["steps"][0]
    assert step["step_number"] == 1
    assert step["tool"] == "create_task"
    assert step["status"] == "success"
    assert step["duration_ms"] >= 0
    assert json.loads(step["arguments_json"]) == {"title": "buy milk"}


def test_multi_step_success_stores_ordered_steps(client, monkeypatch):
    _enable_planning(monkeypatch)
    response = _fake_plan_chat_response(
        [
            {"tool": "create_task", "arguments": {"title": "buy milk"}},
            {"tool": "list_tasks", "arguments": {}},
        ]
    )
    monkeypatch.setattr(ollama_planner_provider, "_call_ollama_plan", lambda payload: response)

    data = _execute(client, "Create a task to buy milk and then show me all tasks")

    detail = client.get(f"/agent/runs/{data['run_id']}").json()
    assert detail["status"] == "success"
    assert detail["is_multi_step"] is True
    assert len(detail["steps"]) == 2
    assert detail["steps"][0]["step_number"] == 1
    assert detail["steps"][0]["tool"] == "create_task"
    assert detail["steps"][0]["status"] == "success"
    assert detail["steps"][1]["step_number"] == 2
    assert detail["steps"][1]["tool"] == "list_tasks"
    assert detail["steps"][1]["status"] == "success"


def test_clarification_required_creates_run_with_no_step(client):
    data = _execute(client, "Delete a task")

    detail = client.get(f"/agent/runs/{data['run_id']}").json()
    assert detail["status"] == "clarification_required"
    assert detail["steps"] == []


def test_confirmation_required_creates_run_with_no_step(client):
    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]

    data = _execute(client, f"Delete task {task_id}")

    detail = client.get(f"/agent/runs/{data['run_id']}").json()
    assert detail["status"] == "confirmation_required"
    assert detail["steps"] == []


def test_confirmation_followup_creates_new_run_same_conversation(client):
    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]
    asked = _execute(client, f"Delete task {task_id}")

    confirmed = _execute(client, "yes", conversation_id=asked["conversation_id"])

    assert confirmed["run_id"] != asked["run_id"]
    assert confirmed["conversation_id"] == asked["conversation_id"]

    detail = client.get(f"/agent/runs/{confirmed['run_id']}").json()
    assert detail["status"] == "success"
    assert detail["conversation_id"] == asked["conversation_id"]
    assert len(detail["steps"]) == 1
    assert detail["steps"][0]["tool"] == "delete_task"


def test_cancellation_is_stored(client):
    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]
    asked = _execute(client, f"Delete task {task_id}")

    cancelled = _execute(client, "no", conversation_id=asked["conversation_id"])

    detail = client.get(f"/agent/runs/{cancelled['run_id']}").json()
    assert detail["status"] == "cancelled"
    assert detail["steps"] == []


def test_no_matching_tool_is_stored_as_no_tool(client):
    data = _execute(client, "asdkfjaslkdfj this matches nothing at all")

    detail = client.get(f"/agent/runs/{data['run_id']}").json()
    assert detail["status"] == "no_tool"
    assert detail["selected_tool"] is None
    assert detail["steps"] == []


def test_failed_tool_execution_stores_error(client):
    data = _execute(client, "Mark task 999 as done")

    detail = client.get(f"/agent/runs/{data['run_id']}").json()
    assert detail["status"] == "error"
    assert detail["error"] == "Task 999 was not found."
    assert len(detail["steps"]) == 1
    assert detail["steps"][0]["status"] == "error"


def test_partial_multi_step_plan_stores_partial(client, monkeypatch):
    _enable_planning(monkeypatch)
    response = _fake_plan_chat_response(
        [
            {"tool": "create_task", "arguments": {"title": "buy milk"}},
            {"tool": "mark_task_done", "arguments": {"task_id": 999}},
        ]
    )
    monkeypatch.setattr(ollama_planner_provider, "_call_ollama_plan", lambda payload: response)

    data = _execute(client, "Create a task to buy milk and then mark task 999 done")

    detail = client.get(f"/agent/runs/{data['run_id']}").json()
    assert detail["status"] == "partial"
    assert len(detail["steps"]) == 2
    assert detail["steps"][0]["status"] == "success"
    assert detail["steps"][1]["status"] == "error"


def test_list_runs_returns_recent_runs_ordered_and_respects_limit(client):
    _execute(client, "Add a task to buy milk")
    _execute(client, "Add a task to walk the dog")
    _execute(client, "Show me all tasks")

    runs = client.get("/agent/runs").json()
    assert len(runs) == 3
    assert [r["message"] for r in runs] == [
        "Show me all tasks",
        "Add a task to walk the dog",
        "Add a task to buy milk",
    ]

    limited = client.get("/agent/runs", params={"limit": 2}).json()
    assert len(limited) == 2


def test_list_runs_limit_out_of_range_is_rejected(client):
    too_low = client.get("/agent/runs", params={"limit": 0})
    assert too_low.status_code == 422

    too_high = client.get("/agent/runs", params={"limit": 101})
    assert too_high.status_code == 422


def test_unknown_run_id_returns_404(client):
    response = client.get(f"/agent/runs/{uuid.uuid4()}")
    assert response.status_code == 404


def test_run_ids_are_unique(client):
    first = _execute(client, "Add a task to buy milk")
    second = _execute(client, "Add a task to walk the dog")

    assert first["run_id"] != second["run_id"]
    uuid.UUID(first["run_id"])
    uuid.UUID(second["run_id"])


def test_trace_records_the_authenticated_user_as_owner(client, new_db_session, test_user_id):
    import app.db_models as db_models

    data = _execute(client, "Add a task to buy milk")

    run = new_db_session.query(db_models.AgentRun).filter_by(run_id=uuid.UUID(data["run_id"])).one()
    assert run.user_id == test_user_id


def test_cross_user_run_list_is_isolated(client, other_user_headers):
    _execute(client, "Add a task to buy milk")
    other_response = client.post(
        "/agent/execute", json={"message": "Add a task to walk the dog"}, headers=other_user_headers
    )
    assert other_response.status_code == 200

    alice_runs = client.get("/agent/runs").json()
    assert len(alice_runs) == 1
    assert alice_runs[0]["message"] == "Add a task to buy milk"

    bob_runs = client.get("/agent/runs", headers=other_user_headers).json()
    assert len(bob_runs) == 1
    assert bob_runs[0]["message"] == "Add a task to walk the dog"


def test_cross_user_run_detail_returns_404(client, other_user_headers):
    data = _execute(client, "Add a task to buy milk")

    own = client.get(f"/agent/runs/{data['run_id']}")
    cross_user = client.get(f"/agent/runs/{data['run_id']}", headers=other_user_headers)
    nonexistent = client.get(f"/agent/runs/{uuid.uuid4()}", headers=other_user_headers)

    assert own.status_code == 200
    assert cross_user.status_code == 404
    assert nonexistent.status_code == 404
    assert cross_user.json() == nonexistent.json()


def test_tracing_failure_does_not_undo_successful_task_operation(client, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("simulated tracing failure")

    monkeypatch.setattr(agent_trace_service, "AgentRun", boom)

    data = _execute(client, "Add a task to buy milk")

    assert data["selected_tool"] == "create_task"
    assert data["result"]["title"] == "buy milk"
    # The task operation is durable and correct regardless of the
    # tracing failure above (read via a plain, untouched REST endpoint,
    # not the tracing-dependent /agent/runs endpoints).
    assert client.get("/tasks").json()[0]["title"] == "buy milk"

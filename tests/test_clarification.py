"""Tests for safe clarification handling on POST /agent/execute.

When the intended tool is known but a required argument is missing, the
endpoint must ask a deterministic question and must not execute the
tool (no task_service/weather_service call, no task mutation).
"""

from types import SimpleNamespace

import app.services.agent_decision as agent_decision
import app.services.anthropic_decision_provider as anthropic_decision_provider
import app.services.ollama_decision_provider as ollama_decision_provider
from app.services import clarification
from app.services.conversation_memory import PendingClarification
from app.services.task_resolution import TaskCandidate


def _execute(client, message):
    response = client.post("/agent/execute", json={"message": message})
    assert response.status_code == 200
    return response.json()


def test_create_task_missing_title_needs_clarification(client):
    data = _execute(client, "Create a task")

    assert data["selected_tool"] == "create_task"
    assert data["result"] is None
    assert data["needs_clarification"] is True
    assert data["clarification_question"] == "What should the task title be?"
    assert data["final_answer"] == data["clarification_question"]
    assert data["reason"] == "The create_task tool requires title."

    # No task was created.
    assert client.get("/tasks").json() == []


def test_delete_task_missing_task_id_needs_clarification(client):
    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]

    data = _execute(client, "Delete a task")

    assert data["selected_tool"] == "delete_task"
    assert data["result"] is None
    assert data["needs_clarification"] is True
    assert data["clarification_question"] == "Which task ID should I delete?"

    # The existing task was not deleted.
    tasks = client.get("/tasks").json()
    assert any(task["id"] == task_id for task in tasks)


def test_mark_task_done_missing_task_id_needs_clarification(client):
    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]

    data = _execute(client, "Mark a task as done")

    assert data["selected_tool"] == "mark_task_done"
    assert data["needs_clarification"] is True
    assert data["clarification_question"] == "Which task ID should I mark as done?"

    # The task is still not done.
    tasks = client.get("/tasks").json()
    task = next(task for task in tasks if task["id"] == task_id)
    assert task["done"] is False


def test_update_task_missing_title_needs_clarification(client):
    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]

    data = _execute(client, f"Update task {task_id}")

    assert data["selected_tool"] == "update_task"
    assert data["needs_clarification"] is True
    assert data["clarification_question"] == "What should the new title be?"

    # The title is unchanged.
    tasks = client.get("/tasks").json()
    task = next(task for task in tasks if task["id"] == task_id)
    assert task["title"] == "buy milk"


def test_update_task_missing_id_and_title_needs_clarification(client):
    data = _execute(client, "Update a task")

    assert data["selected_tool"] == "update_task"
    assert data["needs_clarification"] is True
    assert data["clarification_question"] == (
        "Which task ID would you like to update, and what should the new title be?"
    )


def test_get_weather_missing_city_needs_clarification(client):
    data = _execute(client, "What is the weather?")

    assert data["selected_tool"] == "get_weather"
    assert data["result"] is None
    assert data["needs_clarification"] is True
    assert data["clarification_question"] == "Which city would you like the weather for?"


def test_list_tasks_never_needs_clarification(client):
    data = _execute(client, "Show me all tasks")

    assert data["selected_tool"] == "list_tasks"
    assert data["needs_clarification"] is False
    assert data["clarification_question"] is None
    assert data["result"] == []


def test_complete_valid_request_still_executes(client):
    data = _execute(client, "Add a task to buy milk")

    assert data["selected_tool"] == "create_task"
    assert data["needs_clarification"] is False
    assert data["clarification_question"] is None
    assert data["result"]["title"] == "buy milk"
    assert data["final_answer"] == 'Created task #1: "buy milk".'


def test_unmatched_message_does_not_need_clarification(client):
    data = _execute(client, "hello there")

    assert data["selected_tool"] is None
    assert data["needs_clarification"] is False
    assert data["clarification_question"] is None


def test_anthropic_missing_argument_needs_clarification_without_fallback(client, monkeypatch):
    # Create the task first, before switching to the mocked LLM provider,
    # so this setup step still goes through the normal rule-based flow.
    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]

    response = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", id="toolu_1", name="delete_task", input={})]
    )

    class _FakeMessages:
        def create(self, **kwargs):
            return response

    class _FakeClient:
        def __init__(self):
            self.messages = _FakeMessages()

    monkeypatch.setattr(agent_decision, "DECISION_PROVIDER", "anthropic")
    monkeypatch.setattr(anthropic_decision_provider, "_get_client", lambda: _FakeClient())

    data = _execute(client, "Delete a task")

    assert data["selected_tool"] == "delete_task"
    assert data["needs_clarification"] is True
    assert data["clarification_question"] == "Which task ID should I delete?"
    assert any(task["id"] == task_id for task in client.get("/tasks").json())


def test_ollama_missing_argument_needs_clarification_without_fallback(client, monkeypatch):
    # Create the task first, before switching to the mocked LLM provider,
    # so this setup step still goes through the normal rule-based flow.
    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]

    response = {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "delete_task", "arguments": {}}}],
        },
        "done": True,
    }

    monkeypatch.setattr(agent_decision, "DECISION_PROVIDER", "ollama")
    monkeypatch.setattr(ollama_decision_provider, "_call_ollama", lambda payload: response)

    data = _execute(client, "Delete a task")

    assert data["selected_tool"] == "delete_task"
    assert data["needs_clarification"] is True
    assert data["clarification_question"] == "Which task ID should I delete?"
    assert any(task["id"] == task_id for task in client.get("/tasks").json())


def test_llm_only_formulation_preserves_selected_tool_over_fallback(client, monkeypatch):
    # "I no longer need one of my tasks" contains neither "delete" nor
    # "remove task", so the rule-based provider alone would recognize no
    # tool at all (selected_tool=None). A model that understands this
    # means delete_task should have its selection preserved, not lost to
    # a fallback that would silently downgrade it to "no tool matched".
    message = "I no longer need one of my tasks"
    assert agent_decision._decide_tool_rule_based(message).selected_tool is None

    response = {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "delete_task", "arguments": {}}}],
        },
        "done": True,
    }
    monkeypatch.setattr(agent_decision, "DECISION_PROVIDER", "ollama")
    monkeypatch.setattr(ollama_decision_provider, "_call_ollama", lambda payload: response)

    data = _execute(client, message)

    assert data["selected_tool"] == "delete_task"
    assert data["needs_clarification"] is True
    assert data["clarification_question"] == "Which task ID should I delete?"


def test_clarification_state_is_isolated_between_users(client, other_user_headers):
    asked = _execute(client, "Create a task")
    conversation_id = asked["conversation_id"]
    assert asked["needs_clarification"] is True

    # Bob reuses alice's conversation_id but has no pending clarification
    # under (bob, conversation_id) - his message is treated as a fresh
    # request, never as an answer to alice's pending create_task.
    bob_response = client.post(
        "/agent/execute",
        json={"message": "Buy milk", "conversation_id": conversation_id},
        headers=other_user_headers,
    )
    assert bob_response.status_code == 200
    bob_data = bob_response.json()
    assert bob_data["selected_tool"] is None
    assert bob_data["needs_clarification"] is False

    # Alice's own later reply still resumes her pending clarification.
    response = client.post(
        "/agent/execute",
        json={"message": "Buy milk", "conversation_id": conversation_id},
    )
    answered = response.json()
    assert answered["needs_clarification"] is False
    assert answered["selected_tool"] == "create_task"
    assert answered["result"]["title"] == "Buy milk"


# --- Ambiguous title-resolution clarification (unit-level) ------------------


def _ambiguous_pending(candidates, tool="delete_task", extra_arguments=None):
    arguments = {"task_id": None, "task_title": "testing"}
    if extra_arguments:
        arguments.update(extra_arguments)
    arguments[clarification.CLARIFICATION_CANDIDATES_KEY] = [
        {"task_id": c.task_id, "title": c.title} for c in candidates
    ]
    return PendingClarification(selected_tool=tool, arguments=arguments, reason="ambiguous", missing=["task_id"])


def test_build_ambiguous_task_question_lists_all_candidates():
    candidates = [TaskCandidate(task_id=3, title="Client presentation"), TaskCandidate(task_id=7, title="Client presentation slides")]
    question = clarification.build_ambiguous_task_question(candidates)

    assert "2 tasks" in question
    assert '#3 "Client presentation"' in question
    assert '#7 "Client presentation slides"' in question


def test_build_not_found_task_question_is_fixed_and_generic():
    question = clarification.build_not_found_task_question()
    assert "couldn't find a task" in question


def test_is_ambiguous_task_clarification_detects_candidate_list():
    ambiguous = _ambiguous_pending([TaskCandidate(task_id=1, title="Testing")])
    ordinary = PendingClarification(selected_tool="delete_task", arguments={"task_id": None}, reason="r", missing=["task_id"])

    assert clarification.is_ambiguous_task_clarification(ambiguous) is True
    assert clarification.is_ambiguous_task_clarification(ordinary) is False


def test_merge_ambiguous_task_reply_resolves_by_list_position():
    candidates = [TaskCandidate(task_id=3, title="Testing"), TaskCandidate(task_id=7, title="Testing")]
    pending = _ambiguous_pending(candidates)

    merged = clarification.merge_ambiguous_task_reply(pending, "2")

    assert merged["task_id"] == 7
    assert clarification.CLARIFICATION_CANDIDATES_KEY not in merged
    assert "task_title" not in merged


def test_merge_ambiguous_task_reply_resolves_by_exact_title_text():
    candidates = [TaskCandidate(task_id=3, title="Client presentation"), TaskCandidate(task_id=7, title="Team presentation")]
    pending = _ambiguous_pending(candidates)

    merged = clarification.merge_ambiguous_task_reply(pending, "team presentation")

    assert merged["task_id"] == 7


def test_merge_ambiguous_task_reply_resolves_by_task_id_digit():
    candidates = [TaskCandidate(task_id=3, title="Testing"), TaskCandidate(task_id=7, title="Testing")]
    pending = _ambiguous_pending(candidates)

    merged = clarification.merge_ambiguous_task_reply(pending, "7")

    assert merged["task_id"] == 7


def test_merge_ambiguous_task_reply_unmatched_reply_keeps_clarification_pending():
    candidates = [TaskCandidate(task_id=3, title="Testing"), TaskCandidate(task_id=7, title="Testing")]
    pending = _ambiguous_pending(candidates)

    merged = clarification.merge_ambiguous_task_reply(pending, "something unrelated")

    assert merged.get("task_id") is None
    assert clarification.CLARIFICATION_CANDIDATES_KEY in merged


def test_existing_missing_argument_merge_reply_is_unaffected():
    # Regression pin: ordinary (non-title) clarification merging is
    # untouched by the ambiguous-title additions above.
    pending = PendingClarification(selected_tool="create_task", arguments={"title": None}, reason="r", missing=["title"])
    merged, needs_clarification_for = clarification.merge_reply(pending, "Buy milk")
    assert merged == {"title": "Buy milk"}
    assert needs_clarification_for == ()


def test_merge_reply_still_prefers_digit_reply_for_task_id():
    pending = PendingClarification(selected_tool="mark_task_done", arguments={"task_id": None}, reason="r", missing=["task_id"])
    merged, needs_clarification_for = clarification.merge_reply(pending, "3")
    assert merged["task_id"] == 3
    assert "task_title" not in merged
    assert needs_clarification_for == ()

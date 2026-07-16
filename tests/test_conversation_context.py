"""Tests for remembered task-reference context ("it"/"that task") on
POST /agent/execute.

Reference resolution must only kick in when the message contains an
explicit, deterministic reference cue - a generic incomplete request
must keep asking for clarification even when context is available.
"""

import pytest

from app.services.clarification import mentions_contextual_reference


def _execute(client, message, conversation_id=None):
    body = {"message": message}
    if conversation_id is not None:
        body["conversation_id"] = str(conversation_id)
    response = client.post("/agent/execute", json=body)
    assert response.status_code == 200
    return response.json()


def test_remembered_context_is_isolated_between_users(client, other_user_headers):
    created = _execute(client, "Add a task to buy milk")
    conversation_id = created["conversation_id"]

    # Bob reuses alice's exact conversation_id. He has no remembered
    # task under (bob, conversation_id), even though alice's context
    # store has one under (alice, conversation_id) - "it" cannot resolve
    # for him, and alice's task is never touched.
    response = client.post(
        "/agent/execute",
        json={"message": "Mark it as done", "conversation_id": conversation_id},
        headers=other_user_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["selected_tool"] == "mark_task_done"
    assert data["needs_clarification"] is True
    assert data["result"] is None

    # Alice's task is unaffected.
    assert client.get(f"/tasks/{created['result']['id']}").json()["done"] is False


def test_mark_it_as_done_uses_remembered_context(client):
    created = _execute(client, "Add a task to buy milk")
    conversation_id = created["conversation_id"]
    task_id = created["result"]["id"]

    data = _execute(client, "Mark it as done", conversation_id=conversation_id)

    assert data["needs_clarification"] is False
    assert data["selected_tool"] == "mark_task_done"
    assert data["result"]["id"] == task_id
    assert data["result"]["done"] is True


def test_delete_that_task_uses_remembered_context(client):
    created = _execute(client, "Add a task to buy milk")
    conversation_id = created["conversation_id"]
    task_id = created["result"]["id"]

    asked = _execute(client, "Delete that task", conversation_id=conversation_id)

    assert asked["needs_clarification"] is False
    assert asked["needs_confirmation"] is True
    assert asked["selected_tool"] == "delete_task"

    data = _execute(client, "yes", conversation_id=conversation_id)

    assert data["selected_tool"] == "delete_task"
    assert data["result"] == {"status": "deleted", "task_id": task_id}
    assert client.get("/tasks").json() == []


def test_update_task_then_mark_it_done_uses_remembered_context(client):
    created = _execute(client, "Add a task to buy milk")
    conversation_id = created["conversation_id"]
    task_id = created["result"]["id"]

    updated = _execute(client, f"Update task {task_id} to Buy oat milk", conversation_id=conversation_id)
    assert updated["result"]["title"] == "Buy oat milk"

    data = _execute(client, "Mark it done", conversation_id=conversation_id)

    assert data["needs_clarification"] is False
    assert data["selected_tool"] == "mark_task_done"
    assert data["result"]["id"] == task_id
    assert data["result"]["done"] is True


@pytest.mark.parametrize(
    "message",
    ["Delete a task", "Mark a task as done", "Update a task"],
)
def test_generic_incomplete_request_still_asks_despite_context(client, message):
    created = _execute(client, "Add a task to buy milk")
    conversation_id = created["conversation_id"]

    data = _execute(client, message, conversation_id=conversation_id)

    assert data["needs_clarification"] is True
    assert data["result"] is None


def test_explicit_task_id_overrides_remembered_context(client):
    created = _execute(client, "Add a task to buy milk")
    conversation_id = created["conversation_id"]

    data = _execute(client, "Mark task 999 as done", conversation_id=conversation_id)

    # Explicit id 999 was used, not the remembered id from task creation.
    assert data["selected_tool"] == "mark_task_done"
    assert data["result"] == {"error": "Task 999 was not found."}


def test_list_tasks_does_not_change_remembered_context(client):
    created = _execute(client, "Add a task to buy milk")
    conversation_id = created["conversation_id"]
    task_id = created["result"]["id"]

    listed = _execute(client, "Show me all tasks", conversation_id=conversation_id)
    assert listed["selected_tool"] == "list_tasks"

    data = _execute(client, "Mark it as done", conversation_id=conversation_id)

    assert data["needs_clarification"] is False
    assert data["result"]["id"] == task_id


def test_referential_message_with_no_context_still_asks(client):
    data = _execute(client, "Mark it as done")

    assert data["needs_clarification"] is True
    assert data["selected_tool"] == "mark_task_done"


def test_two_conversation_ids_do_not_share_remembered_context(client):
    created = _execute(client, "Add a task to buy milk")
    conversation_a = created["conversation_id"]

    data = _execute(client, "Mark it as done")  # fresh conversation_id (conversation B)
    conversation_b = data["conversation_id"]
    assert conversation_b != conversation_a
    assert data["needs_clarification"] is True


def test_deleting_the_remembered_task_clears_the_reference(client):
    created = _execute(client, "Add a task to buy milk")
    conversation_id = created["conversation_id"]

    _execute(client, "Delete it", conversation_id=conversation_id)
    _execute(client, "yes", conversation_id=conversation_id)

    data = _execute(client, "Mark it as done", conversation_id=conversation_id)

    assert data["needs_clarification"] is True


def test_deleting_a_different_explicit_task_does_not_clear_remembered_context(client):
    created = _execute(client, "Add a task to buy milk")
    conversation_id = created["conversation_id"]
    remembered_id = created["result"]["id"]

    # Create a second task via the plain REST endpoint (not through the
    # agent), so remembered context in this conversation still points
    # at the first task.
    other = client.post("/tasks", json={"title": "walk the dog"}).json()
    other_id = other["id"]
    assert other_id != remembered_id

    # Explicitly delete the OTHER (non-remembered) task.
    asked = _execute(client, f"Delete task {other_id}", conversation_id=conversation_id)
    assert asked["needs_confirmation"] is True
    deleted = _execute(client, "yes", conversation_id=conversation_id)
    assert deleted["result"] == {"status": "deleted", "task_id": other_id}

    # The original remembered task is untouched by that deletion, so a
    # referential follow-up should still resolve to it.
    data = _execute(client, "Mark it as done", conversation_id=conversation_id)
    assert data["needs_clarification"] is False
    assert data["result"]["id"] == remembered_id


def test_failed_operation_does_not_update_context(client):
    created = _execute(client, "Add a task to buy milk")
    conversation_id = created["conversation_id"]

    failed = _execute(client, "Mark task 999 as done", conversation_id=conversation_id)
    assert failed["result"] == {"error": "Task 999 was not found."}

    # Context should still be whatever it was before (task 1 from
    # creation) - "it" should resolve to task 1, not 999 or nothing.
    data = _execute(client, "Mark it as done", conversation_id=conversation_id)

    assert data["needs_clarification"] is False
    assert data["result"]["id"] == created["result"]["id"]


def test_unparseable_pending_reply_does_not_use_context(client):
    created = _execute(client, "Add a task to buy milk")
    conversation_id = created["conversation_id"]

    asked = _execute(client, "Delete a task", conversation_id=conversation_id)
    assert asked["needs_clarification"] is True

    reply = _execute(client, "banana", conversation_id=conversation_id)

    assert reply["needs_clarification"] is True
    assert client.get("/tasks").json() != []


def test_referential_pending_reply_uses_context(client):
    created = _execute(client, "Add a task to buy milk")
    conversation_id = created["conversation_id"]
    task_id = created["result"]["id"]

    asked = _execute(client, "Delete a task", conversation_id=conversation_id)
    assert asked["needs_clarification"] is True

    reply = _execute(client, "that one", conversation_id=conversation_id)

    assert reply["needs_clarification"] is False
    assert reply["needs_confirmation"] is True
    assert reply["result"] is None

    confirmed = _execute(client, "yes", conversation_id=conversation_id)
    assert confirmed["result"] == {"status": "deleted", "task_id": task_id}


def test_existing_numeric_pending_reply_flow_still_works(client):
    created = _execute(client, "Add a task to buy milk")
    conversation_id = created["conversation_id"]
    task_id = created["result"]["id"]

    asked = _execute(client, "Delete a task", conversation_id=conversation_id)
    assert asked["needs_clarification"] is True

    answered = _execute(client, str(task_id), conversation_id=conversation_id)

    assert answered["needs_clarification"] is False
    assert answered["needs_confirmation"] is True
    assert answered["result"] is None

    confirmed = _execute(client, "yes", conversation_id=conversation_id)
    assert confirmed["result"] == {"status": "deleted", "task_id": task_id}


@pytest.mark.parametrize(
    "phrase",
    [
        "Șterge-l",
        "Marchează-l ca terminat",
        "Delete acel task",
        "Delete acest task",
        "Delete ultimul task",
        "Mark it as done",
        "Delete that task",
        "Update this task",
        "Delete the previous task",
        "Delete the last task",
        "that one",
        "the previous one",
        "acela",
        "ultimul",
    ],
)
def test_reference_phrases_are_detected_case_insensitively(phrase):
    # Case-insensitivity and diacritics: check both the given casing and
    # an upper-cased variant (Romanian diacritics included).
    assert mentions_contextual_reference(phrase) is True
    assert mentions_contextual_reference(phrase.upper()) is True


@pytest.mark.parametrize(
    "message",
    [
        "Delete a task",
        "Mark a task as done",
        "Update a task",
        "Please edit this",  # contains "edit", must not match "it"
        "Check the item list",  # contains "item", must not match "it"
        "Bring the kit",  # contains "kit", must not match "it"
    ],
)
def test_non_referential_messages_are_not_detected(message):
    assert mentions_contextual_reference(message) is False

"""Tests for the max_length caps added to request schemas
(app/schemas.py): task title/description and agent messages.

Boundary values only - no min_length exists anywhere (an empty agent
message is still accepted, unchanged public behavior; see
test_empty_agent_message_is_still_accepted below), and no response
schema was touched.
"""


def test_task_title_at_the_limit_is_accepted(client):
    response = client.post("/tasks", json={"title": "x" * 200})

    assert response.status_code == 201


def test_task_title_over_the_limit_is_rejected(client):
    response = client.post("/tasks", json={"title": "x" * 201})

    assert response.status_code == 422


def test_task_description_at_the_limit_is_accepted(client):
    response = client.post("/tasks", json={"title": "Buy milk", "description": "x" * 2000})

    assert response.status_code == 201


def test_task_description_over_the_limit_is_rejected(client):
    response = client.post("/tasks", json={"title": "Buy milk", "description": "x" * 2001})

    assert response.status_code == 422


def test_task_update_title_over_the_limit_is_rejected(client):
    created = client.post("/tasks", json={"title": "Buy milk"}).json()

    response = client.patch(f"/tasks/{created['id']}", json={"title": "x" * 201})

    assert response.status_code == 422


def test_task_update_description_over_the_limit_is_rejected(client):
    created = client.post("/tasks", json={"title": "Buy milk"}).json()

    response = client.patch(f"/tasks/{created['id']}", json={"description": "x" * 2001})

    assert response.status_code == 422


def test_agent_execute_message_at_the_limit_is_accepted(client):
    response = client.post("/agent/execute", json={"message": "x" * 4000})

    assert response.status_code == 200


def test_agent_execute_message_over_the_limit_is_rejected(client):
    response = client.post("/agent/execute", json={"message": "x" * 4001})

    assert response.status_code == 422


def test_decide_tool_message_over_the_limit_is_rejected(client):
    response = client.post("/agent/decide-tool", json={"message": "x" * 4001})

    assert response.status_code == 422


def test_empty_agent_message_is_still_accepted(client):
    # No min_length was added - preserves the existing public behavior
    # (an empty message resolves to a harmless no_tool 200, not a 422).
    response = client.post("/agent/execute", json={"message": ""})

    assert response.status_code == 200
    assert response.json()["selected_tool"] is None

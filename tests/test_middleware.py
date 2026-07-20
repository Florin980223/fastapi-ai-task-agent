"""Tests for the request-correlation, access-logging, and
request-body-size-limit middleware (app/middleware.py), and the
logging configuration they rely on (app/logging_config.py).
"""

import logging
import uuid

from app.services import conversation_memory


def test_request_id_is_generated_when_absent(client):
    response = client.get("/tasks")

    request_id = response.headers.get("X-Request-ID")
    assert request_id
    uuid.UUID(request_id)  # generated ids are uuid4 strings


def test_safe_client_supplied_request_id_is_echoed_back(client):
    response = client.get("/tasks", headers={"X-Request-ID": "my-safe-id-123"})

    assert response.headers.get("X-Request-ID") == "my-safe-id-123"


def test_unsafe_client_supplied_request_id_is_replaced(client):
    unsafe = "a" * 200  # longer than the safe pattern allows

    response = client.get("/tasks", headers={"X-Request-ID": unsafe})

    returned = response.headers.get("X-Request-ID")
    assert returned != unsafe
    uuid.UUID(returned)


def test_request_id_with_unsafe_characters_is_replaced(client):
    unsafe = "bad\nid; DROP TABLE tasks;"

    response = client.get("/tasks", headers={"X-Request-ID": unsafe})

    returned = response.headers.get("X-Request-ID")
    assert returned != unsafe
    assert "\n" not in returned
    uuid.UUID(returned)


def test_access_log_line_matches_the_response(client, caplog):
    with caplog.at_level(logging.INFO):
        response = client.get("/tasks")

    request_id = response.headers["X-Request-ID"]
    access_records = [record for record in caplog.records if getattr(record, "path", None) == "/tasks"]

    assert access_records
    record = access_records[-1]
    assert record.method == "GET"
    assert record.status == 200
    assert record.duration_ms >= 0
    assert record.request_id == request_id


def test_user_id_only_logged_after_successful_auth(client, unauthenticated_client, test_user_id, caplog):
    with caplog.at_level(logging.INFO):
        unauthenticated_client.get("/tasks")
        client.get("/tasks")

    records_by_status = {record.status: record for record in caplog.records if hasattr(record, "status")}

    assert records_by_status[401].user_id == "-"
    assert records_by_status[200].user_id == test_user_id


def test_oversized_body_is_rejected_with_413(client):
    response = client.post("/tasks", json={"title": "x" * 70_000})

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}


def test_oversized_body_is_rejected_before_authentication(unauthenticated_client):
    # Proves middleware ordering: the body-size check runs before auth,
    # not after - a client with no/invalid key still gets a clean 413,
    # not a 401.
    response = unauthenticated_client.post("/tasks", json={"title": "x" * 70_000})

    assert response.status_code == 413


def test_oversized_body_response_still_has_a_request_id(client):
    response = client.post("/tasks", json={"title": "x" * 70_000})

    assert response.headers.get("X-Request-ID")


def test_body_within_the_limit_is_delivered_to_the_route_unchanged(client):
    response = client.post("/tasks", json={"title": "Buy milk", "description": "2 liters"})

    assert response.status_code == 201
    assert response.json()["title"] == "Buy milk"
    assert response.json()["description"] == "2 liters"


def test_get_requests_with_no_body_are_unaffected(client):
    response = client.get("/tasks")

    assert response.status_code == 200


def test_request_id_propagates_into_logs_from_deep_inside_a_sync_route(client, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError("simulated persistence failure")

    monkeypatch.setattr(conversation_memory, "set", boom)

    with caplog.at_level(logging.INFO):
        response = client.post("/agent/execute", json={"message": "Delete a task"})

    assert response.status_code == 500
    request_id = response.headers["X-Request-ID"]

    # This warning is logged deep inside routes/agent.py's execute(),
    # running on the sync-route threadpool thread, not the async
    # middleware's own task - proving request_id_var (a contextvar) is
    # correctly propagated across that boundary with zero changes to
    # the warning's own call site.
    warning_records = [
        record for record in caplog.records if record.name == "app.routes.agent" and record.levelname == "WARNING"
    ]
    assert warning_records
    assert warning_records[-1].request_id == request_id


def test_no_raw_api_key_in_any_log_record(client, test_api_key, caplog):
    with caplog.at_level(logging.DEBUG):
        client.post("/tasks", json={"title": "Buy milk"})
        client.get("/tasks")

    for record in caplog.records:
        assert test_api_key not in record.getMessage()

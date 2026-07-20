"""Tests for the generic, catch-all exception handler (app/main.py).

Uses its own TestClient(..., raise_server_exceptions=False): by
default TestClient re-raises an exception that reaches
ServerErrorMiddleware into the test itself (useful for catching bugs
while writing a test, but not what we want here - we want to assert on
the actual 500 Response our handler produces), so these focused tests
build a client with that behavior turned off.
"""

import logging

from fastapi.testclient import TestClient

from app.main import app


def _lenient_client(api_key: str) -> TestClient:
    return TestClient(app, raise_server_exceptions=False, headers={"X-API-Key": api_key})


def test_unexpected_exception_returns_generic_500(monkeypatch, test_api_key, caplog):
    from app.services import task_service

    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure containing a fake secret sauce")

    monkeypatch.setattr(task_service, "list_tasks", boom)

    client = _lenient_client(test_api_key)
    with caplog.at_level(logging.ERROR):
        response = client.get("/tasks")

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal Server Error"}
    assert "secret sauce" not in response.text
    assert "RuntimeError" not in response.text
    assert "Traceback" not in response.text

    # The real exception (with traceback) was logged server-side only,
    # tagged with this request's request_id.
    assert "secret sauce" in caplog.text
    error_records = [record for record in caplog.records if record.levelname == "ERROR"]
    assert error_records
    assert error_records[-1].request_id == response.headers["X-Request-ID"]


def test_unexpected_exception_response_has_no_query_string_or_body_leakage(monkeypatch, test_api_key):
    from app.services import task_service

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(task_service, "create_task", boom)

    client = _lenient_client(test_api_key)
    response = client.post("/tasks", json={"title": "a secret-looking title xyz123"})

    assert response.status_code == 500
    assert "secret-looking" not in response.text


def test_existing_http_exceptions_are_unaffected(client):
    response = client.get("/tasks/999999")

    assert response.status_code == 404
    assert response.json() == {"detail": "Task not found"}


def test_existing_validation_errors_are_unaffected(client):
    response = client.post("/tasks", json={"not_title": "oops"})

    assert response.status_code == 422

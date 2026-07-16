"""Tests for X-API-Key authentication (app/services/auth.py) and its
configuration (app.config.API_KEYS / _parse_api_keys).

Covers: missing/invalid/valid key handling on every protected endpoint,
/health and /agent/tools staying public, strict startup validation of
API_KEYS (empty config, malformed entries, empty keys/user_ids, the
reserved "__unmigrated__" user_id), constant-time comparison actually
being used, and that a raw API key never appears in persisted data,
HTTP responses, or logs.
"""

import logging
import uuid

import pytest

import app.services.auth as auth_module
from app.config import UNMIGRATED_USER_ID, ApiKeyConfigError, _parse_api_keys
from app.db_models import AgentRun, AgentRunStep, Task

# (method, path, json_body) for every endpoint requirement #1 protects.
_PROTECTED_ENDPOINTS = [
    ("GET", "/tasks", None),
    ("POST", "/tasks", {"title": "Buy milk"}),
    ("GET", "/tasks/1", None),
    ("PATCH", "/tasks/1", {"title": "New title"}),
    ("PATCH", "/tasks/1/done", None),
    ("DELETE", "/tasks/1", None),
    ("POST", "/agent/execute", {"message": "Add a task to buy milk"}),
    ("GET", "/agent/runs", None),
    (f"GET", f"/agent/runs/{uuid.uuid4()}", None),
]

_PUBLIC_ENDPOINTS = [
    ("GET", "/health"),
    ("GET", "/agent/tools"),
]


@pytest.mark.parametrize("method,path,body", _PROTECTED_ENDPOINTS)
def test_missing_api_key_is_rejected(unauthenticated_client, method, path, body):
    response = unauthenticated_client.request(method, path, json=body)
    assert response.status_code == 401
    assert response.json()["detail"] == "Missing X-API-Key header"


@pytest.mark.parametrize("method,path,body", _PROTECTED_ENDPOINTS)
def test_invalid_api_key_is_rejected(unauthenticated_client, method, path, body):
    response = unauthenticated_client.request(method, path, json=body, headers={"X-API-Key": "not-a-real-key"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid API key"


def test_valid_api_key_is_accepted(client):
    response = client.get("/tasks")
    assert response.status_code == 200


@pytest.mark.parametrize("method,path", _PUBLIC_ENDPOINTS)
def test_public_endpoints_do_not_require_a_key(unauthenticated_client, method, path):
    response = unauthenticated_client.request(method, path)
    assert response.status_code == 200


def test_authentication_uses_constant_time_comparison(client, monkeypatch):
    calls = []
    original = auth_module.secrets.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return original(a, b)

    monkeypatch.setattr(auth_module.secrets, "compare_digest", spy)

    response = client.get("/tasks")

    assert response.status_code == 200
    assert calls, "get_current_user must authenticate via secrets.compare_digest, not a plain '==' lookup"


def test_invalid_key_error_never_echoes_the_attempted_key(unauthenticated_client):
    secret_candidate = "attempted-invalid-key-should-never-appear"
    response = unauthenticated_client.get("/tasks", headers={"X-API-Key": secret_candidate})

    assert response.status_code == 401
    assert secret_candidate not in response.text


def test_invalid_key_attempt_is_never_logged(unauthenticated_client, caplog):
    secret_candidate = "attempted-invalid-key-should-never-be-logged"
    with caplog.at_level(logging.DEBUG):
        response = unauthenticated_client.get("/tasks", headers={"X-API-Key": secret_candidate})

    assert response.status_code == 401
    for record in caplog.records:
        assert secret_candidate not in record.getMessage()


def test_raw_api_key_never_appears_in_a_successful_response(client, test_api_key):
    response = client.post("/tasks", json={"title": "Buy milk"})
    assert response.status_code == 201
    assert test_api_key not in response.text


def test_raw_api_key_never_persisted_in_tasks_or_runs(client, new_db_session, test_api_key, other_test_api_key):
    TEST_API_KEY, OTHER_TEST_API_KEY = test_api_key, other_test_api_key

    client.post("/tasks", json={"title": "Buy milk"})
    client.post("/agent/execute", json={"message": "Add a task to buy eggs"})

    for row in new_db_session.query(Task).all():
        for value in (row.user_id, row.title, row.description):
            assert value is None or (TEST_API_KEY not in value and OTHER_TEST_API_KEY not in value)

    for row in new_db_session.query(AgentRun).all():
        for value in (row.user_id, row.message, row.selected_tool, row.error, row.decision_provider, row.status):
            assert value is None or (TEST_API_KEY not in value and OTHER_TEST_API_KEY not in value)

    for row in new_db_session.query(AgentRunStep).all():
        for value in (row.tool, row.arguments_json, row.status, row.error, row.result_summary):
            assert value is None or (TEST_API_KEY not in value and OTHER_TEST_API_KEY not in value)


class TestApiKeysConfigValidation:
    """Direct unit tests of app.config._parse_api_keys - the strict
    startup validation for API_KEYS. Deliberately bypasses HTTP/app
    startup entirely so a bad configuration is exercised as a pure
    function call, not by reloading the app module.
    """

    def test_empty_configuration_is_rejected(self):
        with pytest.raises(ApiKeyConfigError):
            _parse_api_keys("")

    def test_whitespace_only_configuration_is_rejected(self):
        with pytest.raises(ApiKeyConfigError):
            _parse_api_keys("   ")

    def test_entry_without_a_colon_is_rejected(self):
        with pytest.raises(ApiKeyConfigError):
            _parse_api_keys("no-colon-here")

    def test_empty_key_is_rejected(self):
        with pytest.raises(ApiKeyConfigError):
            _parse_api_keys(":alice")

    def test_empty_user_id_is_rejected(self):
        with pytest.raises(ApiKeyConfigError):
            _parse_api_keys("somekey:")

    def test_empty_entry_between_commas_is_rejected(self):
        with pytest.raises(ApiKeyConfigError):
            _parse_api_keys("key1:alice,,key2:bob")

    def test_duplicate_key_is_rejected(self):
        with pytest.raises(ApiKeyConfigError):
            _parse_api_keys("samekey:alice,samekey:bob")

    def test_reserved_unmigrated_user_id_is_rejected(self):
        with pytest.raises(ApiKeyConfigError):
            _parse_api_keys(f"somekey:{UNMIGRATED_USER_ID}")

    def test_valid_configuration_parses_correctly(self):
        result = _parse_api_keys("key1:alice,key2:bob")
        assert result == {"key1": "alice", "key2": "bob"}

    def test_validation_errors_never_contain_the_raw_key(self):
        secret_key = "super-secret-value-should-not-leak"

        with pytest.raises(ApiKeyConfigError) as exc_info:
            _parse_api_keys(f"{secret_key}:{UNMIGRATED_USER_ID}")
        assert secret_key not in str(exc_info.value)

        with pytest.raises(ApiKeyConfigError) as exc_info:
            _parse_api_keys(f"{secret_key}:alice,{secret_key}:bob")
        assert secret_key not in str(exc_info.value)

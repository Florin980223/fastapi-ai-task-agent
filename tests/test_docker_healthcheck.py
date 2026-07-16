"""Unit tests for docker/healthcheck.py's check_health() - the pure
function backing the container's HEALTHCHECK instruction.

Loaded by file path (rather than `import docker.healthcheck`) since
docker/ is a plain scripts directory, not a Python package, and this
avoids any ambiguity with a pip-installed `docker` SDK package if one
is ever added. Never starts Docker, a real server, or makes a real
network call - urllib.request.urlopen is monkeypatched throughout.
"""

import importlib.util
import urllib.error
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "docker" / "healthcheck.py"
_spec = importlib.util.spec_from_file_location("docker_healthcheck", _MODULE_PATH)
healthcheck = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(healthcheck)


class _FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_returns_true_on_200(monkeypatch):
    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", lambda url, timeout: _FakeResponse(200))

    assert healthcheck.check_health("http://127.0.0.1:8000/health", timeout=1.0) is True


def test_returns_false_on_non_200_status(monkeypatch):
    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", lambda url, timeout: _FakeResponse(503))

    assert healthcheck.check_health("http://127.0.0.1:8000/health", timeout=1.0) is False


def test_returns_false_on_url_error(monkeypatch):
    def raise_url_error(url, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", raise_url_error)

    assert healthcheck.check_health("http://127.0.0.1:8000/health", timeout=1.0) is False


def test_returns_false_on_timeout(monkeypatch):
    def raise_timeout(url, timeout):
        raise TimeoutError("timed out")

    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", raise_timeout)

    assert healthcheck.check_health("http://127.0.0.1:8000/health", timeout=1.0) is False


def test_uses_module_defaults_when_called_with_no_arguments(monkeypatch):
    seen = {}

    def fake_urlopen(url, timeout):
        seen["url"] = url
        seen["timeout"] = timeout
        return _FakeResponse(200)

    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", fake_urlopen)

    assert healthcheck.check_health() is True
    assert seen["url"] == healthcheck.URL
    assert seen["timeout"] == healthcheck.TIMEOUT_SECONDS

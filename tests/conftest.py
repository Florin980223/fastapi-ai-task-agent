"""Shared pytest fixtures for the test suite."""

import pytest
from fastapi.testclient import TestClient

import app.models as models
from app.main import app


@pytest.fixture
def client():
    """A FastAPI TestClient for making requests against the app."""
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_tasks_db():
    """Reset the in-memory task store before and after every test.

    tasks_db and _next_id are plain module-level globals (see
    app/models.py), so without this they would leak state between
    tests and make results depend on test order.
    """
    models.tasks_db.clear()
    models._next_id = 1
    yield
    models.tasks_db.clear()
    models._next_id = 1

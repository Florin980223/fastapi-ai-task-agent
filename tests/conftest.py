"""Shared pytest fixtures for the test suite."""

import pytest
from fastapi.testclient import TestClient

import app.models as models
import app.services.conversation_memory as conversation_memory
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


@pytest.fixture(autouse=True)
def reset_conversation_memory():
    """Reset pending-clarification memory before and after every test.

    conversation_memory._pending is a module-level global, same reason
    as reset_tasks_db above.
    """
    conversation_memory._pending.clear()
    yield
    conversation_memory._pending.clear()

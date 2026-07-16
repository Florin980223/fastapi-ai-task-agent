"""Isolation for the evaluation runner: a fresh temp-file SQLite database
per run, a full state reset between every case, and a deterministic
weather stand-in - shared by evals/runner.py and this feature's own
pytest tests so isolation is only ever implemented once.

A real temp *file* (not :memory:) is used deliberately: it sidesteps the
"each connection checkout gets its own empty in-memory database" problem
tests/conftest.py has to work around with StaticPool, since a file-based
SQLite database is naturally shared correctly across independent
connections/threads.

Both app.dependency_overrides[get_db] AND app.database.SessionLocal are
overridden - the former for the FastAPI-injected session routes use, the
latter because agent_trace_service.record_execute_run deliberately opens
its own database.SessionLocal() session (see that module's docstring).
Overriding both, rather than relying on "set DATABASE_URL before
app.database is imported", is what makes this correct even when invoked
from inside an already-running pytest process (where app.database was
already imported, and bound, once by tests/conftest.py).
"""

import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

import app.database as database
import app.services.conversation_memory as conversation_memory
from app.database import Base, get_db
from app.main import app
from app.services import weather_service

# A fixed, deterministic weather reading - Open-Meteo's uptime must never
# affect an agent-quality score, in any mode, including live-ollama.
_FAKE_WEATHER_FIELDS = {
    "country": "Evalland",
    "latitude": 0.0,
    "longitude": 0.0,
    "current_temperature": 18.5,
    "wind_speed": 5.0,
    "weather_code": 1,
}


def _fake_get_weather_for_city(city: str) -> dict:
    return {"city": city, **_FAKE_WEATHER_FIELDS}


@contextmanager
def mock_weather_service():
    """Patches weather_service.get_weather_for_city with a deterministic
    fake for the duration of the block, restoring the original after.
    """
    original = weather_service.get_weather_for_city
    weather_service.get_weather_for_city = _fake_get_weather_for_city
    try:
        yield
    finally:
        weather_service.get_weather_for_city = original


def reset_state(engine: Engine) -> None:
    """Full reset between every case: drop+recreate all tables (tasks,
    agent_runs, agent_run_steps - including autoincrement ids, which is
    what makes setup_tasks ids deterministic), and clear conversation
    memory's pending-clarification / pending-confirmation / remembered-
    task-id state.
    """
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    conversation_memory._pending.clear()
    conversation_memory._pending_confirmation.clear()
    conversation_memory._last_task_id.clear()


@dataclass
class IsolatedEnvironment:
    client: TestClient
    engine: Engine


@contextmanager
def isolated_app_client():
    """Yields an IsolatedEnvironment bound to a fresh temp-file SQLite
    database, with get_db and app.database.SessionLocal both overridden
    and get_weather_for_city mocked. Tears everything down - including
    deleting the temp directory - on exit, and restores whatever
    get_db/SessionLocal were bound to before (so this can be nested
    safely inside a pytest session that already has its own overrides).
    """
    tmpdir = tempfile.mkdtemp(prefix="agent_evals_")
    try:
        db_path = Path(tmpdir) / "eval.db"
        engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        eval_session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        def _override_get_db():
            db = eval_session_local()
            try:
                yield db
            finally:
                db.close()

        previous_override = app.dependency_overrides.get(get_db)
        previous_session_local = database.SessionLocal

        Base.metadata.create_all(bind=engine)
        app.dependency_overrides[get_db] = _override_get_db
        database.SessionLocal = eval_session_local

        try:
            with mock_weather_service(), TestClient(app) as client:
                yield IsolatedEnvironment(client=client, engine=engine)
        finally:
            if previous_override is not None:
                app.dependency_overrides[get_db] = previous_override
            else:
                app.dependency_overrides.pop(get_db, None)
            database.SessionLocal = previous_session_local
            engine.dispose()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

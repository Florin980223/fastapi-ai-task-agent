"""Isolation for the evaluation runner: a fresh temp-file SQLite database
per run, a full state reset between every case, and a deterministic
weather stand-in - shared by evals/runner.py and this feature's own
pytest tests so isolation is only ever implemented once.

A real temp *file* (not :memory:) is used deliberately: it sidesteps the
"each connection checkout gets its own empty in-memory database" problem
tests/conftest.py has to work around with StaticPool, since a file-based
SQLite database is naturally shared correctly across independent
connections/threads.

app.dependency_overrides[get_db], app.database.SessionLocal, AND
app.database.engine are all overridden - the first for the
FastAPI-injected session routes use, the second because
agent_trace_service.record_execute_run deliberately opens its own
database.SessionLocal() session (see that module's docstring), and the
third because app.database.init_db() - run on every app startup via the
FastAPI lifespan, which TestClient(app, ...) triggers as a context
manager - now verifies the schema (see app/services/schema_migration.py)
directly against the module-level engine, not through SessionLocal at
all. Overriding all three, rather than relying on "set DATABASE_URL
before app.database is imported", is what makes this correct even when
invoked from inside an already-running pytest process (where
app.database was already imported, and bound, once by
tests/conftest.py) - and, critically, is what keeps every eval run
fully off the real on-disk tasks.db.
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
from app import config
from app.database import Base, get_db
from app.main import app
from app.services import schema_migration, weather_service
from evals import EVAL_API_KEY, EVAL_USER_ID

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
    agent_runs, agent_run_steps, conversation_states - including
    autoincrement ids, which is what makes setup_tasks ids
    deterministic). conversation_memory's pending-clarification /
    pending-confirmation / remembered-task-id state now lives in the
    conversation_states table, registered on the same Base, so dropping
    and recreating all tables already clears it too - no separate reset
    needed.
    """
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


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
        previous_engine = database.engine
        # Mutate (not reassign) the live dict so app.services.auth - which
        # reads config.API_KEYS live on every request - sees this
        # immediately, regardless of what was parsed at import time.
        previous_api_keys = dict(config.API_KEYS)
        config.API_KEYS[EVAL_API_KEY] = EVAL_USER_ID
        # The rate limiter (app/services/rate_limiter.py) must never
        # affect eval results - a growing dataset run back-to-back
        # shouldn't start failing cases just because it crossed a rate
        # limit meant for accidental/abusive HTTP clients, not this
        # runner. Read live by the limiter, like config.API_KEYS above.
        previous_rate_limit_enabled = config.RATE_LIMIT_ENABLED
        config.RATE_LIMIT_ENABLED = False

        # init_db() (run by the FastAPI lifespan below, via TestClient's
        # context manager) is deliberately verify-only now - it never
        # creates or stamps a schema itself (see
        # app/services/schema_migration.py). As an eval temporary
        # database - exactly the kind of caller README.md's "Database
        # migrations (Alembic)" section carves out for
        # Base.metadata.create_all() - this bootstraps the schema
        # itself, then stamps it at head, so init_db()'s verify-only
        # check finds a database it recognizes as current.
        Base.metadata.create_all(bind=engine)
        schema_migration.stamp_head(engine)
        app.dependency_overrides[get_db] = _override_get_db
        database.SessionLocal = eval_session_local
        # init_db() (run by the FastAPI lifespan below) uses this module-
        # level engine directly - without overriding it too, its schema
        # check would run against the real on-disk tasks.db's engine
        # instead of this temp one.
        database.engine = engine

        try:
            with mock_weather_service(), TestClient(app, headers={"X-API-Key": EVAL_API_KEY}) as client:
                yield IsolatedEnvironment(client=client, engine=engine)
        finally:
            if previous_override is not None:
                app.dependency_overrides[get_db] = previous_override
            else:
                app.dependency_overrides.pop(get_db, None)
            database.SessionLocal = previous_session_local
            database.engine = previous_engine
            config.API_KEYS.clear()
            config.API_KEYS.update(previous_api_keys)
            config.RATE_LIMIT_ENABLED = previous_rate_limit_enabled
            engine.dispose()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

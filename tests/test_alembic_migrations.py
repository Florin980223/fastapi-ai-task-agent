"""Tests for the Alembic baseline migration (alembic/versions/0001_baseline.py)
and the startup-time schema verification it enables
(app/services/schema_migration.py, app/database.py::init_db()).

Every test here uses a temp-file SQLite database (tmp_path) - never
tasks.db, and never even app.database.engine's default target unless a
test explicitly monkeypatches it to point at a temp file first. No test
in this file mutates any Alembic Config's sqlalchemy.url globally -
each builds its own Config pointed at its own temp file.

The manual `python -m alembic upgrade head` / `python -m alembic
current` CLI entry points were also verified by hand against a scratch
temp file during development (never tasks.db) - these tests exercise
the same underlying alembic.command API for speed/reliability rather
than spawning a subprocess per test.
"""

import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from app.config import UNMIGRATED_USER_ID
from app.database import Base
from app import db_models  # noqa: F401 - registers all models on Base.metadata
from app.services.db_migrate import backfill_legacy_user_id_columns
from app.services.schema_migration import (
    SchemaNotAdoptedError,
    SchemaOutOfDateError,
    diff_against_baseline,
    ensure_schema_is_current,
    stamp_head_for_tests,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"
_ALEMBIC_DIR = _REPO_ROOT / "alembic"


def _config_for(db_path: Path) -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _engine_for(db_path: Path):
    return create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})


def _legacy_populated_engine(db_path: Path):
    """A pre-authentication-shaped SQLite file with real data in it -
    no user_id column anywhere, no conversation_states table at all -
    shaped like tests/test_db_migrate.py's own _legacy_engine() helper,
    adapted to a file (so it can go through the full adoption
    procedure) and with an explicit "NOT NULL" on id, matching exactly
    what a real legacy database looks like (verified against the real,
    read-only-inspected tasks.db's own DDL: SQLAlchemy's create_all()
    always emits "id INTEGER NOT NULL, ... PRIMARY KEY (id)" - a real
    legacy tasks.db was created that way by an older version of this
    app, not via a bare "INTEGER PRIMARY KEY" the way SQLite's own
    shorthand would reflect it).
    """
    engine = _engine_for(db_path)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE tasks ("
                "id INTEGER NOT NULL, "
                "title VARCHAR NOT NULL, "
                "description VARCHAR, "
                "done BOOLEAN NOT NULL, "
                "PRIMARY KEY (id)"
                ")"
            )
        )
        connection.execute(text("INSERT INTO tasks (title, description, done) VALUES ('Legacy task 1', NULL, 0)"))
        connection.execute(
            text("INSERT INTO tasks (title, description, done) VALUES ('Legacy task 2', '2 liters', 1)")
        )
    return engine


def _table_names(engine) -> set[str]:
    return set(inspect(engine).get_table_names())


# --- 1. Fresh database -------------------------------------------------------


def test_fresh_database_upgrade_to_head_creates_expected_schema(tmp_path):
    db_path = tmp_path / "fresh.db"
    cfg = _config_for(db_path)

    command.upgrade(cfg, "head")

    engine = _engine_for(db_path)
    inspector = inspect(engine)
    tables = _table_names(engine)
    assert tables == {"tasks", "agent_runs", "agent_run_steps", "conversation_states", "alembic_version"}

    task_columns = {c["name"]: c["nullable"] for c in inspector.get_columns("tasks")}
    assert task_columns == {"id": False, "user_id": False, "title": False, "description": True, "done": False}

    run_indexes = {ix["name"]: ix for ix in inspector.get_indexes("agent_runs")}
    # SQLite's inspector reports "unique" as 1/0, not a Python bool.
    assert bool(run_indexes["ix_agent_runs_run_id"]["unique"]) is True
    assert set(run_indexes["ix_agent_runs_run_id"]["column_names"]) == {"run_id"}
    assert bool(run_indexes["ix_agent_runs_conversation_id"]["unique"]) is False
    assert bool(run_indexes["ix_agent_runs_user_id"]["unique"]) is False

    fks = inspector.get_foreign_keys("agent_run_steps")
    assert len(fks) == 1
    assert fks[0]["referred_table"] == "agent_runs"
    assert fks[0]["referred_columns"] == ["run_id"]
    assert fks[0]["constrained_columns"] == ["run_id"]

    unique_constraints = inspector.get_unique_constraints("conversation_states")
    assert len(unique_constraints) == 1
    assert unique_constraints[0]["name"] == "uq_conversation_states_user_conversation"
    assert set(unique_constraints[0]["column_names"]) == {"user_id", "conversation_id"}

    current = MigrationContext.configure(engine.connect()).get_current_revision()
    assert current == "0001_baseline"
    engine.dispose()


def test_upgrade_head_is_idempotent(tmp_path):
    db_path = tmp_path / "idempotent.db"
    cfg = _config_for(db_path)

    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")  # must not raise, must not duplicate anything

    engine = _engine_for(db_path)
    assert _table_names(engine) == {"tasks", "agent_runs", "agent_run_steps", "conversation_states", "alembic_version"}
    engine.dispose()


# --- 2. Legacy pre-auth database (adoption path C) ---------------------------


def test_legacy_database_adoption_preserves_all_rows(tmp_path):
    db_path = tmp_path / "legacy.db"
    engine = _legacy_populated_engine(db_path)

    # Step 2 of the documented adoption procedure: bring the schema
    # fully current using the app's own existing, already-tested logic.
    Base.metadata.create_all(bind=engine)
    backfill_legacy_user_id_columns(engine)
    # backfill_legacy_user_id_columns adds the missing user_id COLUMN
    # but - a genuine, pre-existing gap this test's diff check below
    # caught - never the accompanying INDEX the ORM model declares
    # (Task.user_id has index=True), since create_all() only creates
    # indexes for tables it creates itself, never for a table that
    # already existed via other means (verified separately). Creating
    # any indexes still missing from an already-existing table is
    # therefore its own explicit adoption-procedure step, not a
    # db_migrate.py change - see README.md's adoption guide.
    for table in Base.metadata.tables.values():
        for index in table.indexes:
            index.create(bind=engine, checkfirst=True)

    # Step 3: verify clean before stamping. alembic check/command.check
    # can't be used here - it requires the database to already be
    # stamped at head before it can even run - so this uses the same
    # pre-stamp diff the documented adoption procedure uses.
    assert diff_against_baseline(engine) == []

    # Step 4: adopt - no DDL, no data touched.
    cfg = _config_for(db_path)
    command.stamp(cfg, "head")

    inspector = inspect(engine)
    assert _table_names(engine) == {"tasks", "agent_runs", "agent_run_steps", "conversation_states", "alembic_version"}

    with engine.connect() as connection:
        rows = connection.execute(text("SELECT title, description, done, user_id FROM tasks ORDER BY id")).all()
    assert len(rows) == 2
    assert rows[0].title == "Legacy task 1"
    assert rows[0].description is None
    assert rows[0].done == 0
    assert rows[0].user_id == UNMIGRATED_USER_ID
    assert rows[1].title == "Legacy task 2"
    assert rows[1].description == "2 liters"
    assert rows[1].user_id == UNMIGRATED_USER_ID

    current = MigrationContext.configure(engine.connect()).get_current_revision()
    assert current == "0001_baseline"
    engine.dispose()


# --- 3. Already-current database (adoption path B) ---------------------------


def test_already_current_database_is_verified_and_stamped_without_recreating_tables(tmp_path):
    db_path = tmp_path / "current.db"
    engine = _engine_for(db_path)
    Base.metadata.create_all(bind=engine)

    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tasks (user_id, title, description, done) VALUES ('alice', 'Buy milk', NULL, 0)")
        )

    assert diff_against_baseline(engine) == []  # must already be clean - nothing to fix first

    cfg = _config_for(db_path)
    command.stamp(cfg, "head")  # no DDL, no data touched

    with engine.connect() as connection:
        rows = connection.execute(text("SELECT user_id, title FROM tasks")).all()
    assert len(rows) == 1
    assert rows[0].user_id == "alice"
    assert rows[0].title == "Buy milk"

    current = MigrationContext.configure(engine.connect()).get_current_revision()
    assert current == "0001_baseline"
    engine.dispose()


# --- 4. Downgrade is blocked --------------------------------------------------


def test_downgrade_from_baseline_is_intentionally_blocked(tmp_path):
    db_path = tmp_path / "downgrade.db"
    cfg = _config_for(db_path)
    command.upgrade(cfg, "head")

    engine = _engine_for(db_path)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tasks (user_id, title, description, done) VALUES ('alice', 'Buy milk', NULL, 0)")
        )

    with pytest.raises(RuntimeError, match="destroy all data"):
        command.downgrade(cfg, "base")

    # Nothing was dropped or altered.
    assert _table_names(engine) == {"tasks", "agent_runs", "agent_run_steps", "conversation_states", "alembic_version"}
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT title FROM tasks")).all()
    assert len(rows) == 1
    assert rows[0].title == "Buy milk"
    engine.dispose()


# --- 5. Baseline matches the ORM models, not just eyeballed ------------------


def test_baseline_matches_current_orm_metadata(tmp_path):
    db_path = tmp_path / "diff.db"
    cfg = _config_for(db_path)
    command.upgrade(cfg, "head")

    # This IS the "don't blindly trust autogenerate" check, expressed as
    # a fast local test: if app/db_models.py ever changes without a
    # matching new revision, this fails immediately.
    command.check(cfg)


# --- 6. app.database.init_db() / ensure_schema_is_current --------------------


def test_init_db_on_fresh_unstamped_database_fails_safely_and_changes_nothing(tmp_path):
    db_path = tmp_path / "startup_fresh.db"
    engine = _engine_for(db_path)

    with pytest.raises(SchemaNotAdoptedError, match="python -m alembic upgrade head"):
        ensure_schema_is_current(engine)

    assert _table_names(engine) == set()  # completely untouched
    engine.dispose()


def test_init_db_succeeds_after_alembic_upgrade_head(tmp_path):
    db_path = tmp_path / "startup_upgraded.db"
    engine = _engine_for(db_path)

    with pytest.raises(SchemaNotAdoptedError):
        ensure_schema_is_current(engine)

    command.upgrade(_config_for(db_path), "head")

    ensure_schema_is_current(engine)  # must not raise
    engine.dispose()


def test_init_db_on_already_current_stamped_database_is_a_noop(tmp_path):
    db_path = tmp_path / "startup_noop.db"
    command.upgrade(_config_for(db_path), "head")
    engine = _engine_for(db_path)

    ensure_schema_is_current(engine)  # first call
    ensure_schema_is_current(engine)  # second call - still a silent no-op

    assert _table_names(engine) == {"tasks", "agent_runs", "agent_run_steps", "conversation_states", "alembic_version"}
    engine.dispose()


def test_init_db_on_unadopted_populated_database_fails_without_mutation(tmp_path):
    db_path = tmp_path / "startup_unadopted.db"
    engine = _engine_for(db_path)
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tasks (user_id, title, description, done) VALUES ('alice', 'Buy milk', NULL, 0)")
        )

    with pytest.raises(SchemaNotAdoptedError, match="never adopted"):
        ensure_schema_is_current(engine)

    # Nothing was altered or stamped - still no alembic_version table,
    # and the row inserted above is still exactly as it was.
    assert "alembic_version" not in _table_names(engine)
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT title FROM tasks")).all()
    assert len(rows) == 1 and rows[0].title == "Buy milk"
    engine.dispose()


def test_init_db_on_outdated_database_fails_without_mutation(tmp_path):
    db_path = tmp_path / "startup_outdated.db"
    command.upgrade(_config_for(db_path), "head")
    engine = _engine_for(db_path)

    # Simulate a future revision existing that this database hasn't
    # been upgraded to yet - not reachable through normal means today
    # (there is only one revision), so it's simulated directly.
    fake_future_revision = uuid.uuid4().hex[:12]
    with engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = :rev"), {"rev": fake_future_revision})

    with pytest.raises(SchemaOutOfDateError, match="python -m alembic upgrade head"):
        ensure_schema_is_current(engine)

    with engine.connect() as connection:
        stamped = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert stamped == fake_future_revision  # unchanged - nothing auto-corrected it
    engine.dispose()


# --- 7. stamp_head_for_tests (the test/eval fixture helper) ------------------


def test_stamp_head_for_tests_matches_the_real_upgrade_result(tmp_path):
    stamped_path = tmp_path / "stamped.db"
    upgraded_path = tmp_path / "upgraded.db"

    stamped_engine = _engine_for(stamped_path)
    Base.metadata.create_all(bind=stamped_engine)
    stamp_head_for_tests(stamped_engine)

    command.upgrade(_config_for(upgraded_path), "head")
    upgraded_engine = _engine_for(upgraded_path)

    assert MigrationContext.configure(stamped_engine.connect()).get_current_revision() == MigrationContext.configure(
        upgraded_engine.connect()
    ).get_current_revision()
    ensure_schema_is_current(stamped_engine)  # must not raise
    stamped_engine.dispose()
    upgraded_engine.dispose()

"""Tests for the legacy on-disk-database compatibility patch
(app/services/db_migrate.py).

There's no Alembic in this project - schema is otherwise managed purely
by Base.metadata.create_all(), which never alters an existing table.
backfill_legacy_user_id_columns is the one explicit, narrowly-scoped
ALTER TABLE helper that handles the one known gap: a tasks.db that
already existed before the user_id column was added. These tests build
a raw, pre-authentication-shaped SQLite table by hand (bypassing the
ORM entirely) to prove the helper adds the column, backfills existing
rows with the reserved sentinel, and is a safe no-op on every
subsequent call - exactly the scenario an upgraded, pre-existing
tasks.db would hit on the app's next startup.
"""

from sqlalchemy import create_engine, inspect, text

from app.config import UNMIGRATED_USER_ID
from app.services.db_migrate import backfill_legacy_user_id_columns


def _legacy_engine():
    """A fresh in-memory SQLite database with a `tasks` table shaped
    exactly like the pre-authentication schema: no user_id column at
    all - as if this table had been created by an older version of the
    app, before Task.user_id existed.
    """
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE tasks ("
                "id INTEGER PRIMARY KEY, "
                "title VARCHAR NOT NULL, "
                "description VARCHAR, "
                "done BOOLEAN NOT NULL"
                ")"
            )
        )
        connection.execute(
            text("INSERT INTO tasks (title, description, done) VALUES ('Legacy task', NULL, 0)")
        )
    return engine


def test_adds_missing_user_id_column_and_backfills_sentinel():
    engine = _legacy_engine()

    backfill_legacy_user_id_columns(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("tasks")}
    assert "user_id" in columns

    with engine.connect() as connection:
        row = connection.execute(text("SELECT user_id FROM tasks")).one()
    assert row.user_id == UNMIGRATED_USER_ID


def test_running_twice_is_a_safe_no_op():
    engine = _legacy_engine()

    backfill_legacy_user_id_columns(engine)
    # Second call must not raise (e.g. "duplicate column") and must not
    # change the already-migrated data.
    backfill_legacy_user_id_columns(engine)

    with engine.connect() as connection:
        row = connection.execute(text("SELECT user_id FROM tasks")).one()
    assert row.user_id == UNMIGRATED_USER_ID


def test_table_that_already_has_user_id_is_left_untouched():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE tasks ("
                "id INTEGER PRIMARY KEY, "
                "user_id VARCHAR NOT NULL, "
                "title VARCHAR NOT NULL, "
                "description VARCHAR, "
                "done BOOLEAN NOT NULL"
                ")"
            )
        )
        connection.execute(text("INSERT INTO tasks (user_id, title, done) VALUES ('alice', 'Fresh task', 0)"))

    backfill_legacy_user_id_columns(engine)

    with engine.connect() as connection:
        row = connection.execute(text("SELECT user_id FROM tasks")).one()
    assert row.user_id == "alice"


def test_missing_table_is_a_safe_no_op():
    engine = create_engine("sqlite:///:memory:")
    # No tables at all - must not raise.
    backfill_legacy_user_id_columns(engine)

"""One-shot, idempotent compatibility patch for a pre-authentication
on-disk database.

There is no migration framework (Alembic) in this project - schema is
otherwise managed purely by Base.metadata.create_all(), which only ever
creates missing tables and never alters an existing one. That's exactly
right for a brand-new database (tests, CI, a fresh clone), but a
tasks.db that already existed before the `user_id` column was added
needs one explicit ALTER TABLE. This module handles exactly that one
known gap - if the schema needs to evolve further than this, introduce
Alembic instead of extending this function.

Legacy rows (created before any user existed) are backfilled with the
reserved sentinel app.config.UNMIGRATED_USER_ID rather than a real
user_id, so they become inert/inaccessible via the API instead of
silently landing on whichever real user happens to authenticate first.
See .env.example for how to manually reclaim them.
"""

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app.config import UNMIGRATED_USER_ID

logger = logging.getLogger(__name__)

# Every table that gained a required user_id column when authentication
# was added.
_TABLES_NEEDING_USER_ID = ("tasks", "agent_runs")


def backfill_legacy_user_id_columns(engine: Engine) -> None:
    """For each table in _TABLES_NEEDING_USER_ID: if the table exists but
    has no user_id column yet, add one via ALTER TABLE, backfilling
    every existing row with UNMIGRATED_USER_ID. Safe to call on every
    startup - a table that doesn't exist yet (brand-new database) or
    already has the column (already migrated, or created fresh by
    create_all) is left untouched.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    for table_name in _TABLES_NEEDING_USER_ID:
        if table_name not in existing_tables:
            continue

        columns = {column["name"] for column in inspector.get_columns(table_name)}
        if "user_id" in columns:
            continue

        with engine.begin() as connection:
            connection.execute(
                text(
                    f"ALTER TABLE {table_name} ADD COLUMN user_id VARCHAR NOT NULL "
                    f"DEFAULT '{UNMIGRATED_USER_ID}'"
                )
            )
        logger.info(
            "Added missing user_id column to '%s' - existing rows were assigned the "
            "reserved sentinel user_id (see .env.example to reclaim them).",
            table_name,
        )


def backfill_legacy_task_priority_and_due_date_columns(engine: Engine) -> None:
    """Same idea as backfill_legacy_user_id_columns, for the `priority`/
    `due_date` columns added to `tasks` in alembic/versions/0002_*.

    A genuinely pre-Alembic legacy tasks.db predates both user_id AND
    these two columns - Base.metadata.create_all() never alters an
    already-existing table, so the one-time adopt-legacy procedure needs
    its own explicit ALTER TABLE for these too, exactly like user_id.
    Safe to call on every startup/adoption attempt: a table that doesn't
    exist yet, or already has both columns, is left untouched.
    """
    inspector = inspect(engine)
    if "tasks" not in set(inspector.get_table_names()):
        return

    columns = {column["name"] for column in inspector.get_columns("tasks")}

    with engine.begin() as connection:
        if "priority" not in columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN priority VARCHAR NOT NULL DEFAULT 'medium'"))
            logger.info("Added missing priority column to 'tasks' - existing rows were assigned 'medium'.")
        if "due_date" not in columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN due_date DATE"))
            logger.info("Added missing due_date column to 'tasks' - existing rows were left unset (NULL).")

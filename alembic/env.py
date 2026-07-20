"""Alembic environment script.

Never hardcodes a database path or credentials (requirement: this file
must read the runtime DATABASE_URL safely). _get_url() below prefers an
explicit sqlalchemy.url set programmatically on the Config object (used
by app.services.schema_migration.stamp_head_for_tests, which points
Alembic at a specific test/eval engine from inside an already-running
Python process) and otherwise falls back to app.config.DATABASE_URL -
the exact same environment variable / .env file the application itself
reads via app/config.py, so there is exactly one place a database URL
is ever configured. Running `python -m alembic ...` from the repo root
therefore targets exactly what `uvicorn app.main:app` would target.

Importing app.db_models registers all four ORM classes (Task, AgentRun,
AgentRunStep, ConversationState) on Base.metadata before it's used as
target_metadata below - the same `from app import db_models  # noqa`
idiom app/database.py's own init_db() uses.

render_as_batch=True is set in both offline and online migration
contexts. Not needed by the 0001_baseline revision itself (it's pure
CREATE TABLE), but SQLite can't perform most ALTER TABLE operations
directly (drop/alter a column, add a constraint, ...) - Alembic's batch
mode works around this by rebuilding the table under the hood.
Configuring it here means every future migration gets that
SQLite-compatible behavior automatically via `op.batch_alter_table(...)`,
without anyone having to remember it per revision file.

Deliberately does NOT call logging.config.fileConfig() (present in
Alembic's default generated template): fileConfig() reconfigures
Python's *global* root logger handlers from alembic.ini's [loggers]
section - since app.services.schema_migration.stamp_head_for_tests can
run this env.py from inside an already-running application/test
process, that would silently strip out app.logging_config's own
request-id-aware handler the moment any test or the app itself invokes
an Alembic command. Alembic's own internal log messages (e.g. "Running
upgrade -> ...") still work fine without it - they just flow through
whatever logging configuration is already active in the process (the
app's own, when invoked that way; Python's default otherwise).
"""

from sqlalchemy import engine_from_config, pool

from alembic import context

config = context.config

from app.database import Base  # noqa: E402
from app import db_models  # noqa: E402,F401 - registers all models on Base.metadata

target_metadata = Base.metadata


def _get_url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    from app.config import DATABASE_URL

    return DATABASE_URL


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL, no live connection)."""
    context.configure(
        url=_get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (against a live connection)."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _get_url()
    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

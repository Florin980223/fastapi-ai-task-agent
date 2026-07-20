"""Alembic-aware schema verification for application startup.

From this module's ensure_schema_is_current() on down, Alembic is the
ONLY thing that ever creates, alters, migrates, or stamps a "real"
application database's schema - dev, Docker, and production startup
(app.database.init_db()) all go through ensure_schema_is_current(),
which only ever VERIFIES and raises. It never runs `alembic upgrade
head` (or `stamp`, or any other mutating command) on the app's behalf.
A human running `python -m alembic upgrade head` themselves is the only
thing that changes a real application database's schema from here on.
See README.md's "Database migrations (Alembic)" section for the exact
adoption procedures for a fresh, already-current, or legacy database.

Base.metadata.create_all() still exists and is still used directly,
but only for isolated pytest databases, eval temporary databases, and
the dedicated test/eval fixtures below (stamp_head_for_tests) - never
as a path a normal application startup can reach.
"""

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"
_ALEMBIC_DIR = _REPO_ROOT / "alembic"


class SchemaNotAdoptedError(RuntimeError):
    """Raised when a database is empty or has tables but was never
    stamped with Alembic. The app refuses to start rather than
    silently creating, altering, or stamping an unknown schema - see
    README.md's "Database migrations (Alembic)" section for the
    one-time adoption steps.
    """


class SchemaOutOfDateError(RuntimeError):
    """Raised when a database's stamped Alembic revision doesn't match
    the latest available migration. Run `python -m alembic upgrade
    head` before starting the app.
    """


def _alembic_config() -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    return cfg


def _head_revision() -> str | None:
    script = ScriptDirectory.from_config(_alembic_config())
    return script.get_current_head()


def _current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        return context.get_current_revision()


def ensure_schema_is_current(engine: Engine) -> None:
    """Verify-only: never creates, alters, migrates, or stamps
    anything against `engine`. Called by app.database.init_db() on
    every real startup.

    - Empty database (no tables at all) -> SchemaNotAdoptedError. Not
      yet migrated; run `python -m alembic upgrade head` first.
    - Tables exist, no alembic_version table -> SchemaNotAdoptedError.
      Has data but was never adopted by Alembic; run the documented
      adoption procedure first.
    - Tables exist, alembic_version present, but not at head ->
      SchemaOutOfDateError. Run `python -m alembic upgrade head`.
    - Tables exist, alembic_version present, at head -> returns
      normally (silent no-op), same as today's "safe to call on every
      startup" promise, now verifying instead of mutating.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    if "alembic_version" not in existing_tables:
        if not existing_tables:
            raise SchemaNotAdoptedError(
                "Database is empty - it has not been migrated yet. Run "
                "`python -m alembic upgrade head`, then start the app. "
                "See README.md's 'Database migrations (Alembic)' section."
            )
        raise SchemaNotAdoptedError(
            "Database has tables but was never adopted by Alembic. Refusing "
            "to start rather than silently altering its schema. See "
            "README.md's 'Database migrations (Alembic)' section for the "
            "one-time adoption steps."
        )

    current = _current_revision(engine)
    head = _head_revision()
    if current != head:
        raise SchemaOutOfDateError(
            f"Database schema is at revision {current!r}, not the latest "
            f"({head!r}). Run `python -m alembic upgrade head` before "
            "starting the app."
        )


def diff_against_baseline(engine: Engine) -> list:
    """Structural diff between `engine`'s live database and the current
    ORM metadata (Base.metadata) - independent of whether the database
    has ever been stamped by Alembic. An empty list means the live
    schema already matches exactly what the baseline migration would
    produce.

    This is the pre-stamp verification step for adopting an existing
    or legacy database (see README.md's "Database migrations
    (Alembic)" section) - unlike the `alembic check` CLI command (and
    command.check() below), which requires the database to already be
    stamped at head before it can even run (it first checks that the
    database's current revision heads match the script directory's
    heads, and raises "Target database is not up to date" otherwise),
    this works on a database that has never been touched by Alembic at
    all. `alembic check` remains the right tool for ongoing drift
    detection AFTER a database is adopted (see the CI step and
    tests/test_alembic_migrations.py's
    test_baseline_matches_current_orm_metadata, both of which run it
    only after an upgrade/stamp has already happened).
    """
    from app import db_models  # noqa: F401 - registers all models on Base.metadata
    from app.database import Base

    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        return compare_metadata(context, Base.metadata)


def stamp_head_for_tests(engine: Engine) -> None:
    """Records `engine`'s database as already being at the latest
    Alembic revision, without running any DDL.

    NOT used by ensure_schema_is_current()/init_db() above - this is
    only for test/eval fixtures that bootstrap a throwaway database via
    Base.metadata.create_all() directly and then trigger the real
    FastAPI lifespan (which calls init_db()) against it - see
    tests/conftest.py's restart_client_factory and
    evals/isolation.py's isolated_app_client(). Without this,
    ensure_schema_is_current() would correctly, but unhelpfully for a
    test, refuse to proceed against a table-having-but-unstamped
    database, since init_db() itself is never allowed to stamp
    anything on its own.
    """
    cfg = _alembic_config()
    cfg.set_main_option("sqlalchemy.url", str(engine.url))
    command.stamp(cfg, "head")


if __name__ == "__main__":
    # `python -m app.services.schema_migration` - the pre-stamp
    # verification step (step 3) of the adoption procedures in
    # README.md's "Database migrations (Alembic)" section. Reads
    # DATABASE_URL exactly like the app itself (see app/database.py).
    import sys

    from app.database import engine as _app_engine

    _diff = diff_against_baseline(_app_engine)
    if _diff:
        print("Schema does NOT match the baseline migration:")
        for _change in _diff:
            print(f"  {_change}")
        sys.exit(1)
    print("Schema matches the baseline migration exactly - safe to run `alembic stamp head`.")

"""Alembic-aware schema verification for application startup, plus a
safe, explicit CLI for checking and adopting an existing SQLite database
into Alembic (`python -m app.services.schema_migration --help`).

From ensure_schema_is_current() on down, Alembic is the ONLY thing that
ever creates, alters, migrates, or stamps a "real" application
database's schema - dev, Docker, and production startup
(app.database.init_db()) all go through ensure_schema_is_current(),
which only ever VERIFIES and raises. It never runs `alembic upgrade
head` (or `stamp`, or any other mutating command) on the app's behalf.
A human running `python -m alembic upgrade head` themselves - or, for a
legacy pre-Alembic database, the `adopt-legacy` CLI subcommand below -
is the only thing that changes a real application database's schema
from here on. See README.md's "Database migrations (Alembic)" section
for the exact adoption procedures for a fresh, already-current, or
legacy database.

Base.metadata.create_all() still exists and is still used directly,
but only for isolated pytest databases, eval temporary databases, the
dedicated test/eval fixtures below (stamp_head), and inside
adopt-legacy's own explicit, reviewed transformation - never as a path
a normal application startup can reach on its own.
"""

import argparse
import hashlib
import sys
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"
_ALEMBIC_DIR = _REPO_ROOT / "alembic"

# The three tables that can already exist, populated, on a legacy
# pre-Alembic database - conversation_states never predates Alembic
# adoption in practice, so it's never part of the "preserve existing
# rows" safety check below (create_all() only ever creates it fresh).
_LEGACY_PRE_EXISTING_TABLES = ("tasks", "agent_runs", "agent_run_steps")


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
    (Alembic)" section, and the `check`/`adopt-legacy` CLI subcommands
    below) - unlike the `alembic check` CLI command (and command.check()),
    which requires the database to already be stamped at head before it
    can even run (it first checks that the database's current revision
    heads match the script directory's heads, and raises "Target
    database is not up to date" otherwise), this works on a database
    that has never been touched by Alembic at all. `alembic check`
    remains the right tool for ongoing drift detection AFTER a database
    is adopted (see the CI step and
    tests/test_alembic_migrations.py::test_baseline_matches_current_orm_metadata,
    both of which run it only after an upgrade/stamp has already
    happened).
    """
    from app import db_models  # noqa: F401 - registers all models on Base.metadata
    from app.database import Base

    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        return compare_metadata(context, Base.metadata)


def stamp_head(engine: Engine) -> None:
    """Records `engine`'s database as already being at the latest
    Alembic revision, without running any DDL.

    Two callers, both deliberate about never letting
    ensure_schema_is_current()/init_db() do this themselves:
    - Test/eval fixtures that bootstrap a throwaway database via
      Base.metadata.create_all() directly and then trigger the real
      FastAPI lifespan (which calls init_db()) against it - see
      tests/conftest.py's restart_client_factory and
      evals/isolation.py's isolated_app_client(). Without this,
      ensure_schema_is_current() would correctly, but unhelpfully for a
      test, refuse to proceed against a table-having-but-unstamped
      database.
    - This module's own `adopt-legacy` CLI subcommand, as the final
      step of a legacy database's adoption - only ever called after
      diff_against_baseline(engine) has already returned [] and the
      pre-existing data has already been verified unchanged.
    """
    cfg = _alembic_config()
    cfg.set_main_option("sqlalchemy.url", str(engine.url))
    command.stamp(cfg, "head")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _format_diff_entry(entry) -> str:
    """A concise, bounded one-line summary of a single autogenerate
    diff tuple - the raw tuples compare_metadata()/diff_against_baseline()
    return include full SQLAlchemy Table/Index/Column reprs, hundreds of
    characters each, which is not "concise" for a human reading CLI
    output.
    """
    op_type = entry[0]
    target = entry[1] if len(entry) > 1 else None

    if op_type in ("add_table", "remove_table") and hasattr(target, "name"):
        return f"{op_type}: {target.name}"

    if op_type in ("add_index", "remove_index") and hasattr(target, "name"):
        columns = ", ".join(column.name for column in target.columns)
        table_name = target.table.name if target.table is not None else "?"
        return f"{op_type}: {target.name} on {table_name}({columns})"

    if op_type in ("add_constraint", "remove_constraint") and hasattr(target, "name"):
        return f"{op_type}: {target.name}"

    # Column-level and any other op shape: bounded fallback rather than
    # dumping a full object repr.
    text_form = f"{op_type}: {entry[1:]}"
    return text_form if len(text_form) <= 200 else text_form[:197] + "..."


def _read_only_sqlite_engine(path: Path) -> Engine:
    """A SQLite connection that structurally cannot write to `path` -
    refuses to connect at all (rather than silently creating an empty
    file) if `path` doesn't already exist. Used by `check` so "make no
    writes" is a property of the connection itself, not just of the
    code that happens to run over it.
    """
    return create_engine(f"sqlite:///file:{path.as_posix()}?mode=ro&uri=true")


def _read_write_sqlite_engine(path: Path) -> Engine:
    return create_engine(f"sqlite:///{path.as_posix()}", connect_args={"check_same_thread": False})


def _snapshot_preexisting_tables(engine: Engine, table_names: tuple[str, ...]) -> dict[str, dict]:
    """Row-count + full per-row snapshot (ordered by id) of whichever
    of `table_names` currently exist, using their *current* column
    list. Used by adopt-legacy to prove its transformation only ever
    adds columns/tables/indexes and never touches an existing row's
    existing values.
    """
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    snapshot: dict[str, dict] = {}
    with engine.connect() as connection:
        for name in table_names:
            if name not in existing:
                continue
            columns = [column["name"] for column in inspector.get_columns(name)]
            column_list = ", ".join(columns)
            rows = connection.execute(text(f"SELECT {column_list} FROM {name} ORDER BY id")).all()
            snapshot[name] = {"columns": columns, "rows": [tuple(row) for row in rows]}
    return snapshot


def _verify_preexisting_data_unchanged(engine: Engine, before: dict[str, dict]) -> list[str]:
    """Re-fetches each table in `before` using its *original* column
    list (so a newly-added column like user_id is never part of the
    comparison) and compares row-for-row against the snapshot taken
    before the transformation. Returns a list of human-readable
    problems - empty means every pre-existing row's pre-existing
    values are exactly unchanged.
    """
    problems: list[str] = []
    with engine.connect() as connection:
        for table_name, snapshot in before.items():
            columns = snapshot["columns"]
            column_list = ", ".join(columns)
            after_rows = [
                tuple(row) for row in connection.execute(text(f"SELECT {column_list} FROM {table_name} ORDER BY id")).all()
            ]
            if len(after_rows) != len(snapshot["rows"]):
                problems.append(
                    f"{table_name}: row count changed ({len(snapshot['rows'])} -> {len(after_rows)})"
                )
            elif after_rows != snapshot["rows"]:
                problems.append(f"{table_name}: one or more existing column values changed")
    return problems


def _cmd_check(database_path: Path) -> int:
    database_path = database_path.resolve()
    if not database_path.exists():
        print(f"Database file not found: {database_path}")
        return 1

    try:
        engine = _read_only_sqlite_engine(database_path)
        try:
            diff = diff_against_baseline(engine)
        finally:
            engine.dispose()
    except Exception as exc:  # never leak a raw traceback for a CLI safety tool
        print(f"Error inspecting {database_path}: {exc}")
        return 1

    if not diff:
        print(f"{database_path}: schema matches the baseline migration exactly.")
        return 0

    print(f"{database_path}: schema does NOT match the baseline migration:")
    for entry in diff:
        print(f"  {_format_diff_entry(entry)}")
    return 1


def _cmd_adopt_legacy(database_path: Path, backup_path: Path) -> int:
    database_path = database_path.resolve()
    backup_path = backup_path.resolve()

    if not database_path.exists():
        print(f"Database file not found: {database_path}")
        return 1
    if not backup_path.exists():
        print(f"Backup file not found: {backup_path}. Create a backup before adopting - refusing to proceed.")
        return 1

    database_hash = _sha256(database_path)
    backup_hash = _sha256(backup_path)
    if database_hash != backup_hash:
        print(
            f"Backup does not match the database "
            f"(sha256 {backup_hash[:12]}... != {database_hash[:12]}...). The backup "
            "must be a byte-identical copy taken immediately before adoption - "
            "refusing to proceed. If the database has changed since your last "
            "backup (e.g. a previous adoption attempt already ran), take a fresh "
            "backup of its current state first."
        )
        return 1

    engine = _read_write_sqlite_engine(database_path)
    try:
        before = _snapshot_preexisting_tables(engine, _LEGACY_PRE_EXISTING_TABLES)

        try:
            from app import db_models  # noqa: F401 - registers all models on Base.metadata
            from app.database import Base
            from app.services.db_migrate import (
                backfill_legacy_task_priority_and_due_date_columns,
                backfill_legacy_user_id_columns,
            )

            Base.metadata.create_all(bind=engine)
            backfill_legacy_user_id_columns(engine)
            backfill_legacy_task_priority_and_due_date_columns(engine)
            for table in Base.metadata.tables.values():
                for index in table.indexes:
                    index.create(bind=engine, checkfirst=True)
        except Exception as exc:
            print(f"Error while transforming {database_path}: {exc}")
            print("Nothing was stamped. Restore from your backup if you want to abandon this attempt.")
            return 1

        problems = _verify_preexisting_data_unchanged(engine, before)
        if problems:
            print(f"Refusing to stamp {database_path} - existing data changed unexpectedly:")
            for problem in problems:
                print(f"  {problem}")
            return 1

        diff = diff_against_baseline(engine)
        if diff:
            print(f"Refusing to stamp {database_path} - schema still does not match the baseline:")
            for entry in diff:
                print(f"  {_format_diff_entry(entry)}")
            return 1

        stamp_head(engine)
    finally:
        engine.dispose()

    print(f"Adoption complete: {database_path} is now stamped at {_head_revision()}.")
    return 0


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.services.schema_migration",
        description=(
            "Safe, explicit tools for checking and adopting an existing SQLite "
            "database into Alembic. Every database path must be passed "
            "explicitly - this never reads DATABASE_URL or defaults to tasks.db."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_parser = subparsers.add_parser(
        "check", help="Read-only structural diff between a database and the baseline migration."
    )
    check_parser.add_argument(
        "--database-path", required=True, type=Path, help="Path to the SQLite database file to inspect. Never written to."
    )

    adopt_parser = subparsers.add_parser(
        "adopt-legacy",
        help=(
            "Adopt an existing pre-Alembic database: add missing user_id/priority/"
            "due_date columns, missing indexes, create conversation_states, verify, "
            "and stamp at head."
        ),
    )
    adopt_parser.add_argument(
        "--database-path",
        required=True,
        type=Path,
        help="Path to the SQLite database file to adopt. Modified in place - back it up first.",
    )
    adopt_parser.add_argument(
        "--backup-path",
        required=True,
        type=Path,
        help=(
            "Path to a byte-identical backup of --database-path, taken immediately "
            "before running this command. Adoption refuses to proceed unless this "
            "file exists and its SHA-256 matches --database-path exactly."
        ),
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.command == "check":
        return _cmd_check(args.database_path)
    if args.command == "adopt-legacy":
        return _cmd_adopt_legacy(args.database_path, args.backup_path)

    parser.error(f"Unknown command: {args.command!r}")  # pragma: no cover - unreachable, subparsers are exhaustive
    return 2


if __name__ == "__main__":
    sys.exit(main())

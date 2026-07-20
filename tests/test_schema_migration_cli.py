"""Tests for the `check`/`adopt-legacy` CLI
(`python -m app.services.schema_migration ...`, app/services/schema_migration.py).

Every test here builds its own temp-file SQLite database via tmp_path
and drives the CLI through app.services.schema_migration.main(argv) -
never a subprocess, never DATABASE_URL, never tasks.db. No test in this
file ever constructs an engine against, or even references the path of,
the real tasks.db.
"""

import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from app.config import UNMIGRATED_USER_ID
from app.database import Base
from app import db_models  # noqa: F401 - registers all models on Base.metadata
from app.services.schema_migration import diff_against_baseline, main


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_legacy_db(path: Path) -> None:
    """A pre-authentication-shaped SQLite file with real data in it -
    same shape as the real tasks.db, re-verified read-only earlier this
    session: tasks/agent_runs/agent_run_steps exist with no user_id
    column anywhere, agent_runs/agent_run_steps already have their
    non-user_id indexes and FK, and conversation_states doesn't exist
    at all.
    """
    conn = sqlite3.connect(str(path))
    try:
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE tasks ("
            "id INTEGER NOT NULL, title VARCHAR NOT NULL, description VARCHAR, "
            "done BOOLEAN NOT NULL, PRIMARY KEY (id))"
        )
        cur.execute(
            "CREATE TABLE agent_runs ("
            "id INTEGER NOT NULL, run_id CHAR(32) NOT NULL, conversation_id CHAR(32) NOT NULL, "
            "message VARCHAR NOT NULL, decision_provider VARCHAR NOT NULL, is_multi_step BOOLEAN NOT NULL, "
            "status VARCHAR NOT NULL, selected_tool VARCHAR, started_at DATETIME NOT NULL, "
            "finished_at DATETIME NOT NULL, duration_ms INTEGER NOT NULL, error VARCHAR, "
            "PRIMARY KEY (id))"
        )
        cur.execute("CREATE UNIQUE INDEX ix_agent_runs_run_id ON agent_runs (run_id)")
        cur.execute("CREATE INDEX ix_agent_runs_conversation_id ON agent_runs (conversation_id)")
        cur.execute(
            "CREATE TABLE agent_run_steps ("
            "id INTEGER NOT NULL, run_id CHAR(32) NOT NULL, step_number INTEGER NOT NULL, "
            "tool VARCHAR NOT NULL, arguments_json VARCHAR NOT NULL, status VARCHAR NOT NULL, "
            "duration_ms INTEGER NOT NULL, error VARCHAR, result_summary VARCHAR, "
            "PRIMARY KEY (id), FOREIGN KEY(run_id) REFERENCES agent_runs (run_id))"
        )
        cur.execute("CREATE INDEX ix_agent_run_steps_run_id ON agent_run_steps (run_id)")
        cur.execute("INSERT INTO tasks (title, description, done) VALUES ('Legacy task 1', NULL, 0)")
        cur.execute("INSERT INTO tasks (title, description, done) VALUES ('Legacy task 2', '2 liters', 1)")
        conn.commit()
    finally:
        conn.close()


def _write_current_db(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path}")
    try:
        Base.metadata.create_all(bind=engine)
    finally:
        engine.dispose()


# --- check --------------------------------------------------------------


def test_check_is_read_only(tmp_path):
    db_path = tmp_path / "legacy.db"
    _write_legacy_db(db_path)
    before_hash = _sha256(db_path)

    exit_code = main(["check", "--database-path", str(db_path)])

    assert exit_code == 1  # legacy db, diff is non-empty
    assert _sha256(db_path) == before_hash  # not one byte written


def test_check_reports_clean_for_a_fully_current_database(tmp_path):
    db_path = tmp_path / "current.db"
    _write_current_db(db_path)

    exit_code = main(["check", "--database-path", str(db_path)])

    assert exit_code == 0


def test_check_missing_file_fails_clearly(tmp_path):
    db_path = tmp_path / "does_not_exist.db"

    exit_code = main(["check", "--database-path", str(db_path)])

    assert exit_code != 0
    assert not db_path.exists()  # never created as a side effect


def test_check_requires_database_path_argument():
    with pytest.raises(SystemExit) as exc_info:
        main(["check"])
    assert exc_info.value.code == 2


def test_bare_invocation_requires_a_subcommand(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code == 2


def test_top_level_help_lists_both_subcommands(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0

    output = capsys.readouterr().out
    assert "check" in output
    assert "adopt-legacy" in output


# --- adopt-legacy: argument/backup/hash refusals -------------------------


def test_adopt_legacy_requires_database_path_and_backup_path_arguments():
    with pytest.raises(SystemExit) as exc_info:
        main(["adopt-legacy"])
    assert exc_info.value.code == 2

    with pytest.raises(SystemExit) as exc_info:
        main(["adopt-legacy", "--database-path", "somewhere.db"])
    assert exc_info.value.code == 2  # --backup-path still missing


def test_adopt_legacy_refuses_when_backup_missing(tmp_path, capsys):
    db_path = tmp_path / "legacy.db"
    _write_legacy_db(db_path)
    before_hash = _sha256(db_path)
    missing_backup = tmp_path / "no_such_backup.db"

    exit_code = main(
        ["adopt-legacy", "--database-path", str(db_path), "--backup-path", str(missing_backup)]
    )

    assert exit_code == 1
    assert "Backup file not found" in capsys.readouterr().out
    assert _sha256(db_path) == before_hash  # completely untouched


def test_adopt_legacy_refuses_on_backup_hash_mismatch(tmp_path, capsys):
    db_path = tmp_path / "legacy.db"
    _write_legacy_db(db_path)
    before_hash = _sha256(db_path)

    wrong_backup = tmp_path / "wrong.db"
    _write_current_db(wrong_backup)  # a real sqlite file, but not a copy of db_path

    exit_code = main(
        ["adopt-legacy", "--database-path", str(db_path), "--backup-path", str(wrong_backup)]
    )

    assert exit_code == 1
    assert "does not match" in capsys.readouterr().out
    assert _sha256(db_path) == before_hash  # completely untouched


# --- adopt-legacy: the real transformation -------------------------------


def test_adopt_legacy_preserves_all_rows(tmp_path):
    db_path = tmp_path / "legacy.db"
    _write_legacy_db(db_path)
    backup_path = tmp_path / "legacy.db.backup"
    shutil.copy2(db_path, backup_path)

    exit_code = main(
        ["adopt-legacy", "--database-path", str(db_path), "--backup-path", str(backup_path)]
    )
    assert exit_code == 0

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT id, user_id, title, description, done FROM tasks ORDER BY id").fetchall()
    finally:
        conn.close()

    assert rows == [
        (1, UNMIGRATED_USER_ID, "Legacy task 1", None, 0),
        (2, UNMIGRATED_USER_ID, "Legacy task 2", "2 liters", 1),
    ]


def test_adopt_legacy_creates_missing_tables_indexes_constraints(tmp_path):
    db_path = tmp_path / "legacy.db"
    _write_legacy_db(db_path)
    backup_path = tmp_path / "legacy.db.backup"
    shutil.copy2(db_path, backup_path)

    exit_code = main(
        ["adopt-legacy", "--database-path", str(db_path), "--backup-path", str(backup_path)]
    )
    assert exit_code == 0

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        assert set(inspector.get_table_names()) == {
            "tasks",
            "agent_runs",
            "agent_run_steps",
            "conversation_states",
            "alembic_version",
        }

        task_indexes = {ix["name"] for ix in inspector.get_indexes("tasks")}
        assert "ix_tasks_user_id" in task_indexes

        run_indexes = {ix["name"] for ix in inspector.get_indexes("agent_runs")}
        assert "ix_agent_runs_user_id" in run_indexes
        assert "ix_agent_runs_run_id" in run_indexes  # pre-existing, untouched
        assert "ix_agent_runs_conversation_id" in run_indexes  # pre-existing, untouched

        conv_indexes = {ix["name"] for ix in inspector.get_indexes("conversation_states")}
        assert {"ix_conversation_states_user_id", "ix_conversation_states_conversation_id"} <= conv_indexes

        unique_constraints = inspector.get_unique_constraints("conversation_states")
        assert any(uc["name"] == "uq_conversation_states_user_conversation" for uc in unique_constraints)

        fks = inspector.get_foreign_keys("agent_run_steps")
        assert len(fks) == 1
        assert fks[0]["referred_table"] == "agent_runs"
        assert fks[0]["referred_columns"] == ["run_id"]
    finally:
        engine.dispose()


def test_adopt_legacy_leaves_diff_empty_and_stamps_head(tmp_path):
    db_path = tmp_path / "legacy.db"
    _write_legacy_db(db_path)
    backup_path = tmp_path / "legacy.db.backup"
    shutil.copy2(db_path, backup_path)

    exit_code = main(
        ["adopt-legacy", "--database-path", str(db_path), "--backup-path", str(backup_path)]
    )
    assert exit_code == 0

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert diff_against_baseline(engine) == []
        with engine.connect() as connection:
            stamped = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert stamped == "0001_baseline"
    finally:
        engine.dispose()


def test_adopt_legacy_repeated_with_fresh_backup_is_a_safe_noop(tmp_path):
    db_path = tmp_path / "legacy.db"
    _write_legacy_db(db_path)
    first_backup = tmp_path / "legacy.db.backup1"
    shutil.copy2(db_path, first_backup)

    assert main(["adopt-legacy", "--database-path", str(db_path), "--backup-path", str(first_backup)]) == 0

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            rows_before = connection.execute(
                text("SELECT id, user_id, title, description, done FROM tasks ORDER BY id")
            ).all()
    finally:
        engine.dispose()

    fresh_backup = tmp_path / "legacy.db.backup2"
    shutil.copy2(db_path, fresh_backup)  # a fresh, current backup of the now-adopted db

    exit_code = main(
        ["adopt-legacy", "--database-path", str(db_path), "--backup-path", str(fresh_backup)]
    )
    assert exit_code == 0  # safe no-op, not a refusal

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            rows_after = connection.execute(
                text("SELECT id, user_id, title, description, done FROM tasks ORDER BY id")
            ).all()
        assert rows_after == rows_before
        assert diff_against_baseline(engine) == []
    finally:
        engine.dispose()


def test_adopt_legacy_repeated_with_stale_backup_refuses(tmp_path):
    db_path = tmp_path / "legacy.db"
    _write_legacy_db(db_path)
    original_backup = tmp_path / "legacy.db.backup"
    shutil.copy2(db_path, original_backup)

    assert main(["adopt-legacy", "--database-path", str(db_path), "--backup-path", str(original_backup)]) == 0

    hash_after_first_run = _sha256(db_path)

    # Reusing the same (now-stale, pre-transformation) backup a second
    # time must refuse - the database has changed since that backup was
    # taken.
    exit_code = main(
        ["adopt-legacy", "--database-path", str(db_path), "--backup-path", str(original_backup)]
    )

    assert exit_code == 1
    assert _sha256(db_path) == hash_after_first_run  # the refused second attempt changed nothing

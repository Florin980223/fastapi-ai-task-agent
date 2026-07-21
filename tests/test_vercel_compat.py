"""Vercel Compatibility v1 - focused compatibility tests.

Deliberately does not duplicate coverage that already exists elsewhere:
Web UI/authentication behavior against app.main.app is already
exhaustively covered by tests/test_web_ui.py and tests/test_auth.py;
no-automatic-migration-at-startup by tests/test_lifespan.py; sanitized
readiness failures by tests/test_readiness.py; database-backed
conversation/confirmation state by tests/test_confirmation.py,
tests/test_clarification.py, and tests/test_conversation_memory.py.
Since app.index only ever re-exports the exact same app.main.app object
(proven below), all of that existing coverage already applies to the
Vercel entrypoint too - no adapter-specific duplicate is added here.
"""

import os

import pytest
from sqlalchemy.pool import NullPool, QueuePool

from app import config
from app.database import _build_engine
from app.services.db_url_safety import (
    InsecureRemoteDatabaseUrlError,
    MigrationUrlRequiredError,
    PooledUrlForMigrationError,
    is_pooled_neon_url,
    require_ssl_for_remote_postgres,
    resolve_migration_url,
)

FAKE_POOLED_URL = "postgresql+psycopg://demo_user:s3cr3t-pw@ep-fake-pooler.us-east-2.aws.neon.tech/demo?sslmode=require"
FAKE_POOLED_URL_NO_SSL = "postgresql+psycopg://demo_user:s3cr3t-pw@ep-fake-pooler.us-east-2.aws.neon.tech/demo"
FAKE_DIRECT_URL = "postgresql+psycopg://demo_user:s3cr3t-pw@ep-fake.us-east-2.aws.neon.tech/demo?sslmode=require"
FAKE_DIRECT_URL_NO_SSL = "postgresql+psycopg://demo_user:s3cr3t-pw@ep-fake.us-east-2.aws.neon.tech/demo"
FAKE_LOCAL_URL = "postgresql+psycopg://demo_user:s3cr3t-pw@localhost:5432/demo"
FAKE_LOCAL_URL_127 = "postgresql+psycopg://demo_user:s3cr3t-pw@127.0.0.1:5432/demo"
SECRET_MARKER = "s3cr3t-pw"


# --- 1. app/index.py re-exports the exact same app, not a copy --------

def test_app_index_reexports_the_exact_same_app_object():
    from app.index import app as vercel_app
    from app.main import app as main_app

    assert vercel_app is main_app


# --- 2/6. Pooled DATABASE_URL accepted by the runtime ------------------

def test_serverless_pool_mode_accepts_pooled_url_and_uses_nullpool():
    engine = _build_engine(FAKE_POOLED_URL, "serverless")
    try:
        assert isinstance(engine.pool, NullPool)
        assert engine.dialect.driver == "psycopg"
    finally:
        engine.dispose()


def test_serverless_pool_mode_sets_pgbouncer_safe_connect_args():
    engine = _build_engine(FAKE_POOLED_URL, "serverless")
    try:
        # SQLAlchemy stores the connect_args it was given on the dialect;
        # the simplest robust check is constructing again and inspecting
        # the same kwargs this module passes to create_engine - verified
        # indirectly by confirming the engine builds successfully with a
        # sub-second connect_timeout and prepare_threshold=None baked in
        # (a bad/unsupported kwarg would raise at connect time, not at
        # construction time for most DBAPI wrappers, so this is checked
        # by source inspection instead - see app/database.py's
        # _build_engine serverless branch).
        import inspect

        from app import database

        source = inspect.getsource(database._build_engine)
        assert '"prepare_threshold": None' in source
        assert '"connect_timeout": 5' in source
    finally:
        engine.dispose()


# --- 3. Default pool unchanged (regression guard) ----------------------

def test_default_pool_mode_is_unchanged_queuepool():
    engine = _build_engine(FAKE_POOLED_URL.replace("?sslmode=require", ""), "default")
    try:
        assert isinstance(engine.pool, QueuePool)
    finally:
        engine.dispose()


# --- 4. No SQLite fallback in serverless mode --------------------------

def test_serverless_mode_rejects_sqlite_database_url(monkeypatch):
    monkeypatch.setenv("DB_POOL_MODE", "serverless")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./tasks.db")
    monkeypatch.setenv("API_KEYS", "test-key:test-user")

    import importlib

    # RuntimeError (the stable base class), not config.HardeningConfigError:
    # importlib.reload() re-executes config.py's class definitions too, so
    # the exception raised during reload is an instance of a *newly*
    # defined HardeningConfigError class, distinct (by identity) from the
    # one referenced here before the reload ran.
    with pytest.raises(RuntimeError, match="serverless"):
        importlib.reload(config)


@pytest.fixture(autouse=True)
def _reload_config_after_env_mutation():
    """test_serverless_mode_rejects_sqlite_database_url reloads app.config
    in place (it's a module-level singleton, same as every other config
    test in this suite) - restore it to the real test environment
    afterward so later tests never see a stale, error-raising module.
    """
    yield
    import importlib

    importlib.reload(config)


# --- primitive helper unit tests ---------------------------------------

def test_is_pooled_neon_url_detects_pooler_hostname():
    assert is_pooled_neon_url(FAKE_POOLED_URL) is True
    assert is_pooled_neon_url(FAKE_DIRECT_URL) is False


def test_require_ssl_for_remote_postgres_direct():
    require_ssl_for_remote_postgres(FAKE_LOCAL_URL)  # no raise - local host
    with pytest.raises(InsecureRemoteDatabaseUrlError):
        require_ssl_for_remote_postgres(FAKE_DIRECT_URL_NO_SSL)


# --- 5/7/8/9. SSL enforcement ------------------------------------------

def test_remote_serverless_url_without_sslmode_require_is_refused():
    with pytest.raises(InsecureRemoteDatabaseUrlError):
        _build_engine(FAKE_POOLED_URL_NO_SSL, "serverless")


def test_remote_pooled_url_with_sslmode_require_is_accepted():
    engine = _build_engine(FAKE_POOLED_URL, "serverless")
    engine.dispose()  # no raise = accepted


def test_production_migration_url_without_sslmode_require_is_refused():
    with pytest.raises(InsecureRemoteDatabaseUrlError):
        resolve_migration_url("production", FAKE_DIRECT_URL_NO_SSL, "fallback")


def test_production_migration_url_with_sslmode_require_is_accepted():
    result = resolve_migration_url("production", FAKE_DIRECT_URL, "fallback")
    assert result == FAKE_DIRECT_URL


def test_local_postgres_remains_usable_without_ssl_for_serverless_pool_mode():
    engine = _build_engine(FAKE_LOCAL_URL, "serverless")
    engine.dispose()  # no raise = local host may omit sslmode


def test_local_postgres_remains_usable_without_ssl_for_migration_mode():
    result = resolve_migration_url("production", FAKE_LOCAL_URL_127, "fallback")
    assert result == FAKE_LOCAL_URL_127


# --- 10/11/12/13. Migration URL separation ------------------------------

def test_migration_mode_fails_if_migration_database_url_is_absent():
    with pytest.raises(MigrationUrlRequiredError):
        resolve_migration_url("production", None, FAKE_POOLED_URL)


def test_migration_mode_refuses_a_pooled_url():
    with pytest.raises(PooledUrlForMigrationError):
        resolve_migration_url("production", FAKE_POOLED_URL, "fallback")


def test_migration_mode_accepts_a_direct_postgresql_url():
    assert resolve_migration_url("production", FAKE_DIRECT_URL, "fallback") == FAKE_DIRECT_URL


@pytest.mark.parametrize("migration_mode", ["", None, "development", "local"])
def test_non_production_mode_preserves_existing_behavior_exactly(migration_mode):
    fallback = "sqlite:///./tasks.db"
    assert resolve_migration_url(migration_mode, None, fallback) == fallback
    assert resolve_migration_url(migration_mode, FAKE_POOLED_URL, fallback) == fallback


# --- 14. No secret value appears in errors -----------------------------

@pytest.mark.parametrize(
    "trigger",
    [
        lambda: _build_engine(FAKE_POOLED_URL_NO_SSL, "serverless"),
        lambda: resolve_migration_url("production", None, FAKE_POOLED_URL),
        lambda: resolve_migration_url("production", FAKE_POOLED_URL, "fallback"),
        lambda: resolve_migration_url("production", FAKE_DIRECT_URL_NO_SSL, "fallback"),
    ],
)
def test_no_secret_value_appears_in_any_raised_error_message(trigger):
    with pytest.raises(RuntimeError) as exc_info:
        trigger()
    message = str(exc_info.value)
    assert SECRET_MARKER not in message
    assert "ep-fake" not in message


# --- 15. MIGRATION_DATABASE_URL isolation, tested behaviorally ---------

def test_config_has_no_migration_database_url_attribute():
    assert not hasattr(config, "MIGRATION_DATABASE_URL")


@pytest.mark.parametrize("relative_path", ["app/config.py", "app/database.py", "app/main.py"])
def test_runtime_modules_never_reference_migration_database_url(relative_path):
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    source = (repo_root / relative_path).read_text(encoding="utf-8")
    assert "MIGRATION_DATABASE_URL" not in source


def test_alembic_env_is_the_one_place_that_reads_migration_database_url():
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    source = (repo_root / "alembic" / "env.py").read_text(encoding="utf-8")
    assert "MIGRATION_DATABASE_URL" in source


def test_changing_migration_database_url_env_var_does_not_alter_runtime_engine_url(monkeypatch):
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql://should-never-be-used/anything")
    engine = _build_engine(config.DATABASE_URL, "default")
    try:
        assert "should-never-be-used" not in str(engine.url)
    finally:
        engine.dispose()


def test_resolve_migration_url_helper_ignores_environment_and_uses_argument(monkeypatch):
    monkeypatch.setenv("MIGRATION_DATABASE_URL", FAKE_POOLED_URL_NO_SSL)  # deliberately wrong/unused
    result = resolve_migration_url("production", FAKE_DIRECT_URL, "fallback")
    assert result == FAKE_DIRECT_URL
    assert result != FAKE_POOLED_URL_NO_SSL


# --- 16. No secret values embedded in static responses ------------------

def test_static_assets_never_contain_the_configured_api_key(test_api_key):
    from pathlib import Path

    static_dir = Path(__file__).resolve().parent.parent / "app" / "static"
    static_files = list(static_dir.rglob("*"))
    assert static_files, "expected at least one file under app/static/"

    for path in static_files:
        if path.is_file():
            content = path.read_bytes()
            assert test_api_key.encode() not in content


# --- vercel.json / .python-version sanity (config, not app code) -------

def test_vercel_json_excludes_dev_only_directories_but_preserves_runtime_paths():
    import json
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    data = json.loads((repo_root / "vercel.json").read_text(encoding="utf-8"))

    exclude_pattern = data["functions"]["app/index.py"]["excludeFiles"]
    assert "runtime" not in data["functions"]["app/index.py"]

    for must_not_exclude in ("app/static", "alembic.ini", "alembic", "app/main.py", "app/index.py"):
        assert must_not_exclude not in exclude_pattern


def test_python_version_file_declares_312():
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    content = (repo_root / ".python-version").read_text(encoding="utf-8").strip()
    assert content == "3.12"

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@localhost/test")


def test_postgresql_is_default_and_namespace_does_not_depend_on_paths():
    from mnogobase.config import DatabaseSettings, Settings
    from mnogobase.database import database_target

    assert DatabaseSettings().backend == "postgresql"
    a = Settings(database={"backend": "postgresql"}, data_dir="a")
    b = a.model_copy(update={"data_dir": Path("b")})
    assert database_target(a).schema == database_target(b).schema
    assert database_target(a, "service").schema != database_target(a).schema


def test_team_database_namespaces_are_isolated():
    from mnogobase.config import Settings
    from mnogobase.database import database_target
    from mnogobase.service.context import team_settings

    base = Settings(database={"backend": "postgresql"})
    a, b = [team_settings(base, letter * 32) for letter in "ab"]
    assert database_target(a).schema != database_target(b).schema
    assert database_target(a, "service") == database_target(base, "service")


@pytest.mark.parametrize(
    "field,value", [("schema_prefix", "../bad"), ("project", "x; DROP SCHEMA x")]
)
def test_unsafe_database_namespace_is_rejected(field, value):
    from mnogobase.config import DatabaseSettings

    with pytest.raises(ValueError):
        DatabaseSettings(**{field: value})


def test_bind_parameters_preserve_literals_and_cannot_inject_sql():
    from mnogobase.database import bind_sql

    query, params = bind_sql("SELECT '?' AS literal, ? AS value, '100%' AS percent", ("';--?%",))
    assert query == "SELECT '?' AS literal, :p0 AS value, '100%' AS percent"
    assert params == {"p0": "';--?%"}


def test_database_target_hides_password():
    from mnogobase.database import DatabaseTarget

    target = DatabaseTarget("postgresql://user:super-secret@localhost/db", "test")
    assert "super-secret" not in repr(target)


def test_missing_database_url_is_a_clean_cli_error(monkeypatch):
    from typer.testing import CliRunner

    from mnogobase import cli
    from mnogobase.config import Settings

    settings = Settings(database={"backend": "postgresql", "url_env": "MISSING_TEST_DATABASE_URL"})
    monkeypatch.delenv("MISSING_TEST_DATABASE_URL", raising=False)
    monkeypatch.setattr(cli, "load_settings", lambda *args: settings)
    result = CliRunner().invoke(cli.app, ["status"])
    assert result.exit_code == 2
    assert "Set MISSING_TEST_DATABASE_URL" in result.output
    assert "Traceback" not in result.output


def test_sqlite_backend_is_rejected_before_any_store_is_opened():
    from mnogobase.config import DatabaseSettings

    with pytest.raises(ValueError, match="postgresql"):
        DatabaseSettings(backend="sqlite")


def test_file_database_target_is_rejected_without_creating_files(tmp_path):
    from mnogobase.database import DatabaseError, connect

    path = tmp_path / "legacy" / "state.db"
    with pytest.raises(DatabaseError, match="PostgreSQL"):
        connect(path)
    assert not path.parent.exists()

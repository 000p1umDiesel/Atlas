from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@localhost/test")


def test_schema_does_not_depend_on_paths():
    from mnogobase.core.config import Settings
    from mnogobase.core.database import database_target

    a = Settings(data_dir="a")
    b = a.model_copy(update={"data_dir": Path("b")})
    assert database_target(a).schema == database_target(b).schema


def test_projects_have_separate_schemas():
    from mnogobase.core.config import Settings
    from mnogobase.core.database import database_target

    a, b = Settings(database={"project": "alpha"}), Settings(database={"project": "beta"})
    assert database_target(a).schema != database_target(b).schema


@pytest.mark.parametrize(
    "field,value", [("schema_prefix", "../bad"), ("project", "x; DROP SCHEMA x")]
)
def test_unsafe_database_namespace_is_rejected(field, value):
    from mnogobase.core.config import DatabaseSettings

    with pytest.raises(ValueError):
        DatabaseSettings(**{field: value})


def test_bind_parameters_preserve_literals_and_cannot_inject_sql():
    from mnogobase.core.database import bind_sql

    query, params = bind_sql("SELECT '?' AS literal, ? AS value, '100%' AS percent", ("';--?%",))
    assert query == "SELECT '?' AS literal, :p0 AS value, '100%' AS percent"
    assert params == {"p0": "';--?%"}


def test_database_target_hides_password():
    from mnogobase.core.database import DatabaseTarget

    target = DatabaseTarget("postgresql://user:super-secret@localhost/db", "test")
    assert "super-secret" not in repr(target)


def test_missing_database_url_is_a_clean_cli_error(monkeypatch):
    from typer.testing import CliRunner

    from mnogobase import cli
    from mnogobase.core.config import Settings

    settings = Settings(
        database={"url_env": "MISSING_TEST_DATABASE_URL", "password_env": "MISSING_TEST_PG"}
    )
    monkeypatch.delenv("MISSING_TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("MISSING_TEST_PG", raising=False)
    monkeypatch.setattr(cli.common, "load_settings", lambda *args: settings)
    result = CliRunner().invoke(cli.app, ["status"])
    assert result.exit_code == 2
    assert "задайте MISSING_TEST_PG (или MISSING_TEST_DATABASE_URL)" in result.output
    assert "Traceback" not in result.output


def test_url_is_built_from_config_and_password(monkeypatch):
    from mnogobase.core.config import Settings
    from mnogobase.core.database import database_target

    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss/word")
    settings = Settings(database={"host": "postgres", "port": 5433, "name": "kb", "user": "u"})
    url = database_target(settings).url
    assert url == "postgresql+psycopg://u:p%40ss%2Fword@postgres:5433/kb"
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h/db")  # явный DSN важнее
    assert database_target(settings).url == "postgresql://x:y@h/db"

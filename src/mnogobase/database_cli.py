from __future__ import annotations

import sqlite3

import typer
from filelock import Timeout

from mnogobase.database import DatabaseError
from mnogobase.database_migration import migrate_sqlite

app = typer.Typer(no_args_is_help=True, help="Database operations and legacy data migration.")


@app.command("migrate-sqlite")
def migrate() -> None:
    """Перенести SQLite проекта и сервиса в пустые PostgreSQL таблицы; сохранить источники."""
    from mnogobase import cli

    settings = cli._settings()
    try:
        report = migrate_sqlite(settings)
    except (ValueError, DatabaseError, sqlite3.Error, Timeout, OSError) as exc:
        cli._fail_cleanly(exc)
    for name, count in report.items():
        cli.console.print(f"{name}: {count} rows verified")
    cli.console.print("Migration complete. Original SQLite files preserved.")

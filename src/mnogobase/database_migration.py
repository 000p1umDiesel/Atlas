"""Проверяемый перенос SQLite → PostgreSQL без удаления источников."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import ExitStack
from pathlib import Path

from filelock import FileLock

from mnogobase.background.queue import JobQueue
from mnogobase.config import Settings
from mnogobase.database import (
    connect,
    database_lock,
    database_target,
    initialize,
    mark_project,
)
from mnogobase.registry import Registry, ingest_lock
from mnogobase.service.context import team_settings, validate_team_id
from mnogobase.service.store import ServiceStore

REGISTRY_TABLES = (
    "files",
    "stages",
    "chunk_extract",
    "extraction_cache",
    "dirty_entities",
    "meta",
    "pending_removals",
)
SERVICE_TABLES = ("users", "teams", "memberships", "sessions", "conversations", "messages")
QUEUE_TABLES = ("jobs", "desired", "write_state")
ORDERED_TABLES = {"extraction_cache", "conversations", "messages"}


def _snapshot(path: Path, stack: ExitStack):
    # BEGIN IMMEDIATE excludes legacy writers even in WAL mode. No DML/DDL is
    # executed on the source; rollback releases the reservation after verification.
    connection = sqlite3.connect(path, timeout=0, isolation_level=None)
    stack.callback(connection.close)
    connection.execute("BEGIN IMMEDIATE")
    return connection


def _digest(rows):
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        digest.update(json.dumps(list(row), ensure_ascii=False, separators=(",", ":")).encode())
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def _prepare(settings: Settings, kind: str):
    target = database_target(settings, kind)
    if kind == "state":
        store = Registry(target)
        store.close()
    elif kind == "jobs":
        store = JobQueue(target)
        store.close()
    elif kind == "service":
        ServiceStore(target)
    else:
        db = connect(target)
        try:
            initialize(
                db,
                "CREATE TABLE IF NOT EXISTS uploads(doc_id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL)",
                "uploads",
            )
        finally:
            db.close()


def migrate_sqlite(settings: Settings) -> dict[str, int]:
    """Импортировать все текущие SQLite store в пустые целевые таблицы.

    Источники удерживаются под SQLite write reservation; данные всех схем
    PostgreSQL импортируются и сверяются в одной транзакции. API/worker следует
    остановить до команды и запускать после переключения backend.
    """
    if settings.database.backend != "postgresql":
        raise ValueError("Migration requires database.backend: postgresql")
    legacy = settings.model_copy(deep=True)
    legacy.database.backend = "sqlite"
    sources = []
    with ExitStack() as stack:
        settings.service.root.mkdir(parents=True, exist_ok=True)
        stack.enter_context(FileLock(settings.service.root / "worker.lock", timeout=0))
        stack.enter_context(
            database_lock(settings, "worker", settings.service.root / "worker.lock", service=True)
        )
        service_path = database_target(legacy, "service")
        projects = [settings]
        if service_path.is_file():
            source = _snapshot(service_path, stack)
            sources.append((settings, "service", source, SERVICE_TABLES))
            for (team_id,) in source.execute("SELECT id FROM teams ORDER BY id"):
                projects.append(team_settings(settings, validate_team_id(team_id)))
        for project in projects:
            stack.enter_context(FileLock(project.data_dir / "worker.lock", timeout=0))
            stack.enter_context(ingest_lock(project.data_dir))
            stack.enter_context(ingest_lock(project.data_dir, target=database_target(project)))
            sqlite_project = project.model_copy(deep=True)
            sqlite_project.database.backend = "sqlite"
            for kind, tables in [
                ("state", REGISTRY_TABLES),
                ("jobs", QUEUE_TABLES),
                ("uploads", ("uploads",)),
            ]:
                path = database_target(sqlite_project, kind)
                if path.is_file():
                    sources.append((project, kind, _snapshot(path, stack), tables))
        if not sources:
            raise ValueError("No SQLite databases found for this project/service")
        for project, kind, _, _ in sources:
            _prepare(project, kind)
        destination = connect(database_target(settings))
        stack.callback(destination.close)
        report = {}
        with destination:
            # Lock every table before checking emptiness, so concurrent destination
            # writes cannot race the precondition or verification.
            for project, kind, _, tables in sources:
                target = database_target(project, kind)
                for table in tables:
                    qualified = f'"{target.schema}"."{table}"'
                    destination.execute(f"LOCK TABLE {qualified} IN ACCESS EXCLUSIVE MODE")
                    if destination.execute(f"SELECT COUNT(*) FROM {qualified}").fetchone()[0]:
                        raise ValueError(f"Destination must be empty: {target.schema}.{table}")
            for project, kind, source, tables in sources:
                target = database_target(project, kind)
                available = {
                    r[0]
                    for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                for table in tables:
                    if table not in available:
                        continue
                    qualified = f'"{target.schema}"."{table}"'
                    info = source.execute(f'PRAGMA table_info("{table}")').fetchall()
                    columns = [r[1] for r in info]
                    primary = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
                    order = ",".join(f'"{c}"' for c in primary)
                    selection = ",".join(f'"{c}"' for c in columns)
                    if table in ORDERED_TABLES:
                        columns = ["seq", *columns]
                        selection = "rowid," + selection
                    query = f'SELECT {selection} FROM "{table}" ORDER BY {order}'
                    cursor = source.execute(query)
                    names = ",".join(f'"{c}"' for c in columns)
                    placeholders = ",".join("?" for _ in columns)
                    while batch := cursor.fetchmany(1000):
                        destination.executemany(
                            f"INSERT INTO {qualified}({names}) VALUES({placeholders})", batch
                        )
                    expected = _digest(source.execute(query))
                    actual = _digest(
                        destination.execute(f"SELECT {names} FROM {qualified} ORDER BY {order}")
                    )
                    if actual != expected:
                        raise ValueError(f"Migration verification failed: {target.schema}.{table}")
                    report[f"{target.schema}.{table}"] = actual[0]
                    sequence_column = (
                        "seq" if table in ORDERED_TABLES else "id" if table == "jobs" else None
                    )
                    if sequence_column:
                        maximum = destination.execute(
                            f'SELECT MAX("{sequence_column}") FROM {qualified}'
                        ).fetchone()[0]
                        destination.execute(
                            "SELECT setval(pg_get_serial_sequence(?, ?), ?, ?)",
                            (
                                f"{target.schema}.{table}",
                                sequence_column,
                                maximum or 1,
                                maximum is not None,
                            ),
                        )
                if kind == "jobs":
                    # Old queues may predate durable dirty markers.
                    destination.execute(
                        f"INSERT INTO \"{target.schema}\".write_state(path) SELECT DISTINCT path FROM \"{target.schema}\".jobs WHERE attempts>0 AND status IN ('pending','running','failed') ON CONFLICT(path) DO NOTHING"
                    )
        for project in projects:
            if any(
                p.database.project == project.database.project and kind == "state"
                for p, kind, _, _ in sources
            ):
                mark_project(project)
        return report

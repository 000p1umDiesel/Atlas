"""Подключения PostgreSQL и блокировки пространства проекта.

SQL хранилищ использует bind-параметры DB-API; SQLAlchemy компилирует их
для PostgreSQL. Имена схем задаёт только проверенная конфигурация.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from filelock import Timeout
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.pool import NullPool

from mnogobase.config import Settings

INTEGRITY_ERRORS = (IntegrityError,)
OPERATIONAL_ERRORS = (OperationalError,)


class DatabaseError(RuntimeError):
    """Безопасная ошибка: содержит контекст, но не DSN/пароль/SQL."""


@dataclass(frozen=True)
class DatabaseTarget:
    url: str = field(repr=False)
    schema: str
    timeout: int = 5

    def __post_init__(self):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", self.schema):
            raise ValueError("Invalid PostgreSQL schema name")

    def __str__(self):
        return f"PostgreSQL schema {self.schema}"


def database_target(settings: Settings, kind: str = "state") -> DatabaseTarget:
    if kind not in {"state", "jobs", "uploads", "service"}:
        raise ValueError("Unknown database kind")
    url = os.environ.get(settings.database.url_env)
    if not url:
        raise DatabaseError(f"Set {settings.database.url_env} for PostgreSQL")
    suffix = "service" if kind == "service" else f"project_{settings.database.project}"
    return DatabaseTarget(
        url, f"{settings.database.schema_prefix}_{suffix}", settings.database.connect_timeout
    )


def bind_sql(sql: str, params=()):
    """Переводит ? в именованные параметры вне строковых SQL литералов."""
    if isinstance(params, dict):
        return sql, params
    values = iter(params)
    bound = {}

    def parameter(_):
        key = f"p{len(bound)}"
        try:
            bound[key] = next(values)
        except StopIteration:
            raise ValueError("Not enough SQL parameters") from None
        return f":{key}"

    parts = re.split(r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\")", sql)
    for index in range(0, len(parts), 2):
        parts[index] = re.sub(r"\?", parameter, parts[index])
    sentinel = object()
    if next(values, sentinel) is not sentinel:
        raise ValueError("Too many SQL parameters")
    return "".join(parts), bound


class Row(tuple):
    def __new__(cls, values, names):
        row = super().__new__(cls, values)
        row._names = tuple(names)
        return row

    def keys(self):
        return self._names

    def __getitem__(self, key):
        return super().__getitem__(self._names.index(key) if isinstance(key, str) else key)


class Cursor:
    def __init__(self, result):
        self.result = result
        self.rowcount = result.rowcount
        self.names = tuple(result.keys()) if result.returns_rows else ()

    def fetchone(self):
        row = self.result.fetchone()
        return Row(row, self.names) if row is not None else None

    def fetchall(self):
        return [Row(row, self.names) for row in self.result.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


@lru_cache(maxsize=32)
def _engine(url: str, timeout: int):
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
    if not url.startswith("postgresql+psycopg://"):
        raise DatabaseError("DATABASE_URL must use postgresql:// or postgresql+psycopg://")
    return create_engine(
        url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
        connect_args={"connect_timeout": timeout},
        hide_parameters=True,
    )


class PgConnection:
    def __init__(self, target: DatabaseTarget):
        self.target = target
        self.in_transaction = False
        try:
            self.raw = _engine(target.url, target.timeout).connect()
            self.execute(f'SET search_path TO "{target.schema}"')
            self.execute("SET lock_timeout TO '5s'")
        except DBAPIError:
            if hasattr(self, "raw"):
                self.raw.close()
            raise DatabaseError(
                "PostgreSQL unavailable; check DATABASE_URL and database server"
            ) from None

    def execute(self, sql, params=()):
        query, bound = bind_sql(sql, params)
        result = Cursor(self.raw.execute(text(query), bound))
        if sql.strip() == "BEGIN":
            self.in_transaction = True
        elif sql.strip() in {"COMMIT", "ROLLBACK"}:
            self.in_transaction = False
        return result

    def executemany(self, sql, rows):
        for row in rows:
            self.execute(sql, row)

    def executescript(self, script):
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def close(self):
        try:
            if self.in_transaction:
                self.execute("ROLLBACK")
        finally:
            self.raw.close()

    def __enter__(self):
        if not self.in_transaction:
            self.execute("BEGIN")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.execute("ROLLBACK" if exc_type else "COMMIT")


def connect(target: DatabaseTarget) -> PgConnection:
    if not isinstance(target, DatabaseTarget):
        raise DatabaseError(
            "A PostgreSQL DatabaseTarget is required; file databases are unsupported"
        )
    return PgConnection(target)


def initialize(connection, script: str, component: str):
    with connection:
        target = connection.target
        connection.execute("SELECT pg_advisory_xact_lock(?)", (_lock_key(target, "ddl"),))
        connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{target.schema}"')
        connection.executescript(script)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations(component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES(?,1) ON CONFLICT(component) DO NOTHING",
            (component,),
        )


def target_exists(target: DatabaseTarget, table="files") -> bool:
    connection = connect(target)
    try:
        return connection.execute(
            "SELECT to_regclass(?) IS NOT NULL", (f"{target.schema}.{table}",)
        ).fetchone()[0]
    finally:
        connection.close()


def _lock_key(target: DatabaseTarget, key: str) -> int:
    return int.from_bytes(
        hashlib.sha256(f"{target.schema}:{key}".encode()).digest()[:8], signed=True
    )


def transaction_lock(connection, key: str) -> None:
    connection.execute("SELECT pg_advisory_xact_lock(?)", (_lock_key(connection.target, key),))


class PgLock:
    def __init__(self, target: DatabaseTarget, key: str):
        self.target, self.key, self.connection = target, key, None

    @property
    def is_locked(self):
        return self.connection is not None

    def acquire(self):
        connection = connect(self.target)
        try:
            acquired = connection.execute(
                "SELECT pg_try_advisory_lock(?)", (_lock_key(self.target, self.key),)
            ).fetchone()[0]
            if not acquired:
                raise Timeout(self.key)
        except BaseException:
            connection.close()
            raise
        self.connection = connection
        return self

    def release(self):
        if self.connection is not None:
            connection, self.connection = self.connection, None
            try:
                connection.execute(
                    "SELECT pg_advisory_unlock(?)", (_lock_key(self.target, self.key),)
                )
            finally:
                connection.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.release()


def database_lock(settings: Settings, key: str, path: Path, *, service=False):
    target = database_target(settings, "service" if service else "state")
    return PgLock(target, key)


def _project_identity(target: DatabaseTarget) -> dict:
    try:
        url = make_url(target.url).set(password=None)
    except Exception:
        raise DatabaseError("Invalid PostgreSQL DATABASE_URL") from None
    return {"schema": target.schema, "server": hashlib.sha256(str(url).encode()).hexdigest()}


def mark_project(settings: Settings) -> None:
    target = database_target(settings)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    path = settings.data_dir / "postgresql-project.json"
    temporary = path.with_suffix(f".{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(_project_identity(target)), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def project_exists(settings: Settings) -> bool:
    target = database_target(settings)
    marker = settings.data_dir / "postgresql-project.json"
    try:
        if json.loads(marker.read_text(encoding="utf-8")) != _project_identity(target):
            return False
    except (OSError, ValueError):
        return False
    return target_exists(target)

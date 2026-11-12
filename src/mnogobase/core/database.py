"""Подключения PostgreSQL и блокировки пространства проекта.

SQL хранилищ использует bind-параметры DB-API; SQLAlchemy компилирует их
для PostgreSQL. Имена схем задаёт только проверенная конфигурация.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.pool import NullPool

from mnogobase.core.config import Settings
from mnogobase.core.files import write_atomic
from mnogobase.core.log import get_logger

_log = get_logger(__name__)


class DatabaseError(RuntimeError):
    """Безопасная ошибка: содержит контекст, но не DSN/пароль/SQL."""


class LockBusyError(RuntimeError):
    """Advisory lock `key` уже держит другой процесс (второй писатель, второй worker)."""

    def __init__(self, key: str):
        super().__init__(f"Lock {key!r} is held by another process")
        self.key = key


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


def utc_timestamp() -> str:
    """Метка времени для TEXT-колонок: ISO 8601 в UTC с микросекундами, чтобы строки
    сортировались так же, как моменты времени."""
    return datetime.now(UTC).isoformat(timespec="microseconds")


def database_target(settings: Settings) -> DatabaseTarget:
    """Схема проекта `<prefix>_project_<project>`: в ней всё — registry, очередь,
    загрузки, пользователи и учёт LLM."""
    db = settings.database
    url = os.environ.get(db.url_env)
    if not url:
        password = os.environ.get(db.password_env)
        if not password:
            raise DatabaseError(f"Set {db.password_env} (or {db.url_env}) for PostgreSQL")
        url = URL.create(
            "postgresql+psycopg",
            username=db.user,
            password=password,
            host=db.host,
            port=db.port,
            database=db.name,
        ).render_as_string(hide_password=False)
    return DatabaseTarget(
        url,
        f"{settings.database.schema_prefix}_project_{settings.database.project}",
        settings.database.connect_timeout,
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
def _engine(url: str, timeout: int, pooled: bool = True):
    """Пул для коротких операций; advisory lock держит своё соединение без пула, чтобы
    lock жил ровно столько, сколько его владелец."""
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
    if not url.startswith("postgresql+psycopg://"):
        raise DatabaseError("DATABASE_URL must use postgresql:// or postgresql+psycopg://")
    pool = (
        {"pool_size": 5, "max_overflow": 10, "pool_pre_ping": True, "pool_recycle": 1800}
        if pooled
        else {"poolclass": NullPool}
    )
    return create_engine(
        url,
        isolation_level="AUTOCOMMIT",
        **pool,
        # keepalive: тихо оборванное соединение (NAT, idle timeout) иначе держало бы
        # advisory lock только на нашей стороне, и в корпус могли бы писать двое.
        connect_args={
            "connect_timeout": timeout,
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 3,
        },
        hide_parameters=True,
    )


class PgConnection:
    """Соединение с PostgreSQL в схеме проекта, с API в духе sqlite3 (`execute`, `?`).

    Транзакции открываются явным `BEGIN` (или блоком `with connection:`), иначе каждый
    оператор фиксируется сразу (AUTOCOMMIT).

    Долгоживущие владельцы (очередь worker'а, registry) переживают рестарт PostgreSQL:
    оборванное соединение заменяется новым с теми же настройками схемы. Оператор вне
    транзакции, упавший из-за обрыва, повторяется один раз на новом соединении; потерянная
    транзакция не повторяется — её COMMIT завершается ошибкой, а ROLLBACK ничего не делает,
    чтобы не скрыть исходное исключение."""

    def __init__(self, target: DatabaseTarget, *, pooled: bool = True):
        self.target = target
        self.in_transaction = False
        self._pooled = pooled
        self._open()

    def _open(self) -> None:
        """Берёт соединение и задаёт схему проекта; недоступный сервер — DatabaseError."""
        self.raw = None
        try:
            self.raw = _engine(self.target.url, self.target.timeout, self._pooled).connect()
            # Соединение из пула не несёт настроек прежнего владельца.
            self._execute("RESET ALL")
            self._execute(f'SET search_path TO "{self.target.schema}"')
            self._execute("SET lock_timeout TO '5s'")
        except DBAPIError:
            if self.raw is not None:
                with contextlib.suppress(SQLAlchemyError):
                    self.raw.close()
            raise DatabaseError(
                "PostgreSQL unavailable; check the database settings and server"
            ) from None

    def _reconnect(self) -> None:
        with contextlib.suppress(SQLAlchemyError):
            self.raw.close()
        self.in_transaction = False
        self._open()

    def _execute(self, sql, params=()) -> Cursor:
        query, bound = bind_sql(sql, params)
        return Cursor(self.raw.execute(text(query), bound))

    def execute(self, sql, params=()):
        statement = sql.strip()
        if self.raw.invalidated:
            # соединение оборвалось на одном из прошлых операторов
            if self.in_transaction:
                self.in_transaction = False
                if statement == "ROLLBACK":
                    return None  # откатывать нечего: сервер уже отменил транзакцию
                raise DatabaseError(
                    "PostgreSQL connection was lost during a transaction; nothing was committed"
                )
            self._reconnect()
        try:
            result = self._execute(sql, params)
        except DBAPIError as exc:
            if (
                not exc.connection_invalidated
                or self.in_transaction
                or statement in {"COMMIT", "ROLLBACK"}
            ):
                raise
            self._reconnect()  # обрыв вне транзакции: одна попытка на новом соединении
            result = self._execute(sql, params)
        if statement == "BEGIN":
            self.in_transaction = True
        elif statement in {"COMMIT", "ROLLBACK"}:
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


def connect(target: DatabaseTarget, *, pooled: bool = True) -> PgConnection:
    return PgConnection(target, pooled=pooled)


@contextlib.contextmanager
def connection(target: DatabaseTarget) -> Iterator[PgConnection]:
    """Короткое соединение из пула на время блока `with`; закрывается и при ошибке."""
    db = connect(target)
    try:
        yield db
    finally:
        db.close()


def initialize(connection, script: str, component: str, migrations: Sequence[str] = ()):
    """Создаёт таблицы `script` (`IF NOT EXISTS`) и применяет `migrations`, которых ещё нет у
    компонента: версия 1 — исходный `script`, миграция i переводит на версию i + 2. Миграции
    должны быть безопасны и на свежей схеме (script уже даёт новую форму)."""
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
        (version,) = connection.execute(
            "SELECT version FROM schema_migrations WHERE component=?", (component,)
        ).fetchone()
        for statement in migrations[version - 1 :]:
            connection.execute(statement)
        if len(migrations) + 1 > version:
            connection.execute(
                "UPDATE schema_migrations SET version=? WHERE component=?",
                (len(migrations) + 1, component),
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

    def acquire(self):
        connection = connect(self.target, pooled=False)
        try:
            acquired = connection.execute(
                "SELECT pg_try_advisory_lock(?)", (_lock_key(self.target, self.key),)
            ).fetchone()[0]
            if not acquired:
                raise LockBusyError(self.key)
        except BaseException:
            connection.close()
            raise
        self.connection = connection
        return self

    def release(self):
        """Не бросает ошибок БД: закрытие соединения само отпускает session-level lock, а
        ошибка здесь подменила бы исходное исключение блока `with`."""
        if self.connection is not None:
            connection, self.connection = self.connection, None
            try:
                connection.execute(
                    "SELECT pg_advisory_unlock(?)", (_lock_key(self.target, self.key),)
                )
            except (SQLAlchemyError, DatabaseError) as exc:
                _log.warning("lock_release_failed", key=self.key, error_type=type(exc).__name__)
            finally:
                with contextlib.suppress(SQLAlchemyError, DatabaseError):
                    connection.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.release()


def database_lock(settings: Settings, key: str) -> PgLock:
    return PgLock(database_target(settings), key)


def _project_identity(target: DatabaseTarget) -> dict:
    try:
        url = make_url(target.url).set(password=None)
    except Exception:
        raise DatabaseError("Invalid PostgreSQL DATABASE_URL") from None
    return {"schema": target.schema, "server": hashlib.sha256(str(url).encode()).hexdigest()}


def mark_project(settings: Settings) -> None:
    identity = _project_identity(database_target(settings))
    write_atomic(settings.data_dir / "postgresql-project.json", json.dumps(identity))


def project_exists(settings: Settings) -> bool:
    target = database_target(settings)
    marker = settings.data_dir / "postgresql-project.json"
    try:
        if json.loads(marker.read_text(encoding="utf-8")) != _project_identity(target):
            return False
    except (OSError, ValueError):
        return False
    return target_exists(target)

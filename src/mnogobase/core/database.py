"""Подключения PostgreSQL, создание схемы проекта и advisory-блокировки.

Всё служебное состояние проекта — registry стадий ingest, очередь задач, загрузки,
пользователи и сессии API, учёт вызовов LLM — лежит в одной схеме PostgreSQL
`<schema_prefix>_project_<project>` (`database_target`). Хранилища (`stores/registry.py`,
`background/queue.py`, `service/store.py`, `service/observability.py` и др.) получают
`DatabaseTarget`, открывают `PgConnection` и пишут SQL с плейсхолдерами `?` в духе
sqlite3; `bind_sql` превращает их в именованные bind-параметры SQLAlchemy, так что
значения никогда не подставляются в текст запроса. Имя схемы — единственное, что
подставляется в SQL строкой; его задаёт только проверенная конфигурация (шаблон в
`DatabaseTarget`), а не пользовательский ввод.

Короткие операции берут соединения из пула (`pool_pre_ping`, `RESET ALL` при выдаче).
Блокировки — session-level advisory locks PostgreSQL (`PgLock`) на отдельном соединении
без пула с TCP keepalive: блокировка живёт ровно столько, сколько соединение владельца,
и сама снимается при падении процесса. Ключи блокировок: `ingest` (единственный писатель
корпуса, `stores/registry.py:ingest_lock`), `worker`, `graph-analysis`; порядок захвата
graph-analysis → worker → ingest, обратный порядок грозит взаимной блокировкой.

`mark_project` / `project_exists` связывают каталог проекта (`data_dir`) со схемой:
команды только для чтения вне проекта не должны создавать таблицы.
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
from sqlalchemy.exc import DBAPIError, OperationalError, SQLAlchemyError
from sqlalchemy.pool import NullPool

from mnogobase.core.config import Settings
from mnogobase.core.files import write_atomic
from mnogobase.core.log import get_logger

_log = get_logger(__name__)


class DatabaseError(RuntimeError):
    """Безопасная ошибка: содержит контекст, но не DSN/пароль/SQL.

    CLI превращает её в понятное сообщение и код выхода 2, API — в `ServiceError`."""


class LockBusyError(RuntimeError):
    """Advisory lock `key` уже держит другой процесс (второй писатель, второй worker)."""

    def __init__(self, key: str):
        super().__init__(f"Lock {key!r} is held by another process")
        self.key = key


def connection_lost(exc: BaseException) -> bool:
    """Временная потеря связи с PostgreSQL, а не ошибка самого SQL.

    Сюда относятся недоступный сервер и оборванная транзакция (`DatabaseError` из
    `PgConnection`), оборванное соединение (`connection_invalidated`) и `OperationalError`
    (сервер перезапускается, таймаут соединения или блокировки). Ошибки схемы, прав или
    режима «только чтение» — не временные: повтор их не исправит."""
    if isinstance(exc, DatabaseError):
        return True
    return isinstance(exc, DBAPIError) and (
        exc.connection_invalidated or isinstance(exc, OperationalError)
    )


@dataclass(frozen=True)
class DatabaseTarget:
    """Куда подключаться: DSN сервера, схема проекта и таймаут соединения в секундах.

    DSN содержит пароль, поэтому скрыт из `repr`, а `str` показывает только схему.
    Имя схемы проверяется при создании: оно подставляется в SQL строкой, и шаблон
    `[a-z][a-z0-9_]{0,62}` (лимит идентификатора PostgreSQL — 63 байта) исключает
    инъекцию и необходимость экранирования.

    Raises:
        ValueError: имя схемы не подходит под шаблон.
    """

    url: str = field(repr=False)
    schema: str
    timeout: int = 5

    def __post_init__(self):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", self.schema):
            raise ValueError("Недопустимое имя схемы PostgreSQL")

    def __str__(self):
        return f"схема PostgreSQL {self.schema}"


def utc_timestamp() -> str:
    """Метка времени для TEXT-колонок: ISO 8601 в UTC с микросекундами, чтобы строки
    сортировались так же, как моменты времени."""
    return datetime.now(UTC).isoformat(timespec="microseconds")


def database_target(settings: Settings) -> DatabaseTarget:
    """Схема проекта `<prefix>_project_<project>`: в ней всё — registry, очередь,
    загрузки, пользователи и учёт LLM.

    DSN берётся из переменной `database.url_env` (по умолчанию `DATABASE_URL`, её задают
    тесты), иначе собирается из `database.host/port/name/user` и пароля из переменной
    `database.password_env`. Сеть не трогает.

    Raises:
        DatabaseError: нет ни DSN, ни пароля.
    """
    db = settings.database
    url = os.environ.get(db.url_env)
    if not url:
        password = os.environ.get(db.password_env)
        if not password:
            raise DatabaseError(f"Для PostgreSQL задайте {db.password_env} (или {db.url_env})")
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
    """Переводит `?` в именованные параметры (`:p0`, `:p1`, ...) для SQLAlchemy `text()`.

    `?` внутри строковых литералов `'...'` и идентификаторов в кавычках `"..."` не
    трогаются: SQL режется регулярным выражением на литералы и остальной текст, замена
    идёт только в нечётных кусках. Комментарии и dollar-quoted строки не распознаются —
    в них `?` писать нельзя. Словарь `params` считается уже именованным и возвращается
    как есть.

    Возвращает пару (SQL, словарь параметров).

    Raises:
        ValueError: число `?` не совпадает с числом значений.
    """
    if isinstance(params, dict):
        return sql, params
    values = iter(params)
    bound = {}

    def parameter(_):
        key = f"p{len(bound)}"
        try:
            bound[key] = next(values)
        except StopIteration:
            raise ValueError("Не хватает параметров для SQL-запроса") from None
        return f":{key}"

    parts = re.split(r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\")", sql)
    for index in range(0, len(parts), 2):
        parts[index] = re.sub(r"\?", parameter, parts[index])
    sentinel = object()
    if next(values, sentinel) is not sentinel:
        raise ValueError("Слишком много параметров для SQL-запроса")
    return "".join(parts), bound


class Row(tuple):
    """Строка результата: кортеж, который индексируется и номером, и именем колонки,
    как `sqlite3.Row`; `keys()` — имена колонок."""

    def __new__(cls, values, names):
        row = super().__new__(cls, values)
        row._names = tuple(names)
        return row

    def keys(self):
        return self._names

    def __getitem__(self, key):
        return super().__getitem__(self._names.index(key) if isinstance(key, str) else key)


class Cursor:
    """Обёртка над результатом SQLAlchemy с интерфейсом курсора DB-API: `rowcount`,
    `fetchone`, `fetchall`, итерация; строки отдаются как `Row`."""

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
    """Движок SQLAlchemy для DSN; кэшируется, чтобы все соединения процесса делили один пул.

    Пул для коротких операций; advisory lock держит своё соединение без пула, чтобы
    lock жил ровно столько, сколько его владелец. `postgresql://` переписывается на
    драйвер psycopg 3. AUTOCOMMIT: транзакции открываются явным `BEGIN` в `PgConnection`.
    `hide_parameters` не пускает значения параметров в тексты исключений.

    Raises:
        DatabaseError: DSN не PostgreSQL.
    """
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
    if not url.startswith("postgresql+psycopg://"):
        raise DatabaseError(
            "DATABASE_URL должен начинаться с postgresql:// или postgresql+psycopg://"
        )
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
        self._opened: list[bool] = []  # для каждого вложенного `with`: открыл ли он транзакцию
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
            # блокировка строки или таблицы, занятой другим процессом, не вешает команду
            # навсегда: через 5 с оператор падает с ошибкой
            self._execute("SET lock_timeout TO '5s'")
        except DBAPIError:
            if self.raw is not None:
                with contextlib.suppress(SQLAlchemyError):
                    self.raw.close()
            raise DatabaseError(
                "PostgreSQL недоступен: проверьте настройки базы и сервер"
            ) from None

    def _reconnect(self) -> None:
        """Закрывает оборванное соединение и открывает новое с теми же настройками схемы."""
        with contextlib.suppress(SQLAlchemyError):
            self.raw.close()
        self.in_transaction = False
        self._open()

    def _execute(self, sql, params=()) -> Cursor:
        """Выполняет один оператор на текущем соединении, без логики переподключения."""
        query, bound = bind_sql(sql, params)
        return Cursor(self.raw.execute(text(query), bound))

    def execute(self, sql, params=()):
        """Выполняет оператор с параметрами `?` и возвращает `Cursor`.

        Отслеживает `BEGIN` / `COMMIT` / `ROLLBACK` (точное совпадение текста), чтобы знать,
        открыта ли транзакция. Порядок обработки обрыва связи:
        1. соединение уже помечено оборванным: внутри транзакции — `DatabaseError`
           (ROLLBACK молча возвращает `None`), вне транзакции — переподключение;
        2. обрыв во время самого оператора вне транзакции — одна повторная попытка на
           новом соединении; внутри транзакции и для COMMIT/ROLLBACK ошибка пробрасывается,
           потому что неизвестно, что успел зафиксировать сервер.

        Raises:
            DatabaseError: транзакция потеряна из-за обрыва или сервер недоступен.
            sqlalchemy.exc.DBAPIError: ошибка самого оператора.
        """
        statement = sql.strip()
        if self.raw.invalidated:
            # соединение оборвалось на одном из прошлых операторов
            if self.in_transaction:
                self.in_transaction = False
                if statement == "ROLLBACK":
                    return None  # откатывать нечего: сервер уже отменил транзакцию
                raise DatabaseError(
                    "Соединение с PostgreSQL оборвалось посреди транзакции: ничего не записано"
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
        """Выполняет оператор для каждого набора параметров по очереди (без батчинга)."""
        for row in rows:
            self.execute(sql, row)

    def executescript(self, script):
        """Выполняет DDL-скрипт, разбивая его по `;`.

        Разбиение наивное: `;` внутри строк или тел функций сломает скрипт, поэтому
        годится только для простых `CREATE ... IF NOT EXISTS`."""
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def close(self):
        """Откатывает незавершённую транзакцию и возвращает соединение в пул (или
        закрывает его, если оно без пула)."""
        try:
            if self.in_transaction:
                self.execute("ROLLBACK")
        finally:
            self.raw.close()

    def __enter__(self):
        """`with connection:` — транзакция: BEGIN на входе, если она ещё не открыта.

        Вложенный блок (транзакция уже открыта) становится её частью: завершает её только
        тот блок, который её начал."""
        self._opened.append(not self.in_transaction)
        if self._opened[-1]:
            self.execute("BEGIN")
        return self

    def __exit__(self, exc_type, exc, tb):
        """COMMIT при успехе, ROLLBACK при исключении — только во внешнем блоке;
        исключение пробрасывается дальше."""
        if self._opened.pop():
            self.execute("ROLLBACK" if exc_type else "COMMIT")


def connect(target: DatabaseTarget, *, pooled: bool = True) -> PgConnection:
    """Открывает `PgConnection` в схеме `target`; закрывать — вызывающему.

    `pooled=False` — отдельное соединение без пула (для advisory lock).

    Raises:
        DatabaseError: сервер недоступен.
    """
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
    должны быть безопасны и на свежей схеме (script уже даёт новую форму).

    Всё выполняется одной транзакцией под транзакционной advisory-блокировкой `ddl`, так
    что параллельный старт API, worker'а и CLI не применит миграцию дважды. Создаёт схему
    проекта, если её нет; версия компонента хранится в таблице `schema_migrations`.
    `component` — имя владельца таблиц (registry, jobs, service...), версии у каждого свои.
    """
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
    """Есть ли в схеме проекта таблица `table` (`files` — признак созданного registry).

    Ничего не создаёт, поэтому годится для команд только для чтения.

    Raises:
        DatabaseError: сервер недоступен.
    """
    connection = connect(target)
    try:
        return connection.execute(
            "SELECT to_regclass(?) IS NOT NULL", (f"{target.schema}.{table}",)
        ).fetchone()[0]
    finally:
        connection.close()


def _lock_key(target: DatabaseTarget, key: str) -> int:
    """64-битный ключ advisory lock: первые 8 байт sha256 от `<schema>:<key>`.

    Схема входит в ключ, чтобы проекты на одном сервере не блокировали друг друга."""
    return int.from_bytes(
        hashlib.sha256(f"{target.schema}:{key}".encode()).digest()[:8], signed=True
    )


def transaction_lock(connection, key: str) -> None:
    """Транзакционная advisory-блокировка `key`: ждёт освобождения и держится до конца
    текущей транзакции (`pg_advisory_xact_lock`). Вызывать внутри `with connection:`,
    иначе в AUTOCOMMIT она снимется сразу. Очередь так сериализует операции над одним
    путём (`queue-path:<path>`)."""
    connection.execute("SELECT pg_advisory_xact_lock(?)", (_lock_key(connection.target, key),))


class PgLock:
    """Session-level advisory lock PostgreSQL на отдельном соединении без пула.

    Не ждёт: если ключ занят, `acquire` сразу бросает `LockBusyError`. Блокировка снимается
    `release`, закрытием соединения или обрывом связи / смертью процесса — после
    падения писателя не остаётся «вечных» lock-файлов. Используется как контекстный
    менеджер: `with ingest_lock(target): ...`."""

    def __init__(self, target: DatabaseTarget, key: str):
        self.target, self.key, self.connection = target, key, None

    def acquire(self):
        """Пытается взять блокировку (`pg_try_advisory_lock`) и возвращает `self`.

        Raises:
            LockBusyError: блокировку держит другой процесс (соединение при этом закрыто).
            DatabaseError: сервер недоступен.
        """
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

    def held(self) -> bool:
        """Держит ли соединение блокировку прямо сейчас (по `pg_locks` своего backend).

        После обрыва связи сервер снимает session-level lock, а `PgConnection` молча
        переподключается: новое соединение блокировки не держит, и проверка это видит.
        В `pg_locks` 64-битный ключ хранится как `classid` (старшие 32 бита) и `objid`
        (младшие), `objsubid = 1`.

        Raises:
            DatabaseError, sqlalchemy.exc.DBAPIError: сервер недоступен — ответ неизвестен.
        """
        if self.connection is None:
            return False
        key = _lock_key(self.target, self.key) & 0xFFFFFFFFFFFFFFFF
        row = self.connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted "
            "AND pid = pg_backend_pid() AND classid::bigint = ? AND objid::bigint = ? "
            "AND objsubid = 1)",
            (key >> 32, key & 0xFFFFFFFF),
        ).fetchone()
        return bool(row[0])

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
    """`PgLock` с ключом `key` в схеме проекта (`worker`, `graph-analysis`); ещё не взят."""
    return PgLock(database_target(settings), key)


def _project_identity(target: DatabaseTarget) -> dict:
    """Отпечаток базы проекта для маркера: схема и sha256 DSN без пароля.

    Пароль исключён, чтобы его смена не «отвязывала» проект и чтобы он не попал в файл.

    Raises:
        DatabaseError: DSN не разбирается.
    """
    try:
        url = make_url(target.url).set(password=None)
    except Exception:
        raise DatabaseError("Некорректный DATABASE_URL для PostgreSQL") from None
    return {"schema": target.schema, "server": hashlib.sha256(str(url).encode()).hexdigest()}


def mark_project(settings: Settings) -> None:
    """Пишет маркер `<data_dir>/postgresql-project.json`: этот каталог работает с этой
    схемой на этом сервере. Вызывается из `build_app`, то есть при первой команде,
    которая собирает приложение. Запись атомарная."""
    identity = _project_identity(database_target(settings))
    write_atomic(settings.data_dir / "postgresql-project.json", json.dumps(identity))


def project_exists(settings: Settings) -> bool:
    """Есть ли проект: маркер в `data_dir` совпадает с текущей базой и в схеме есть
    таблица `files`.

    Маркер защищает от ложного «проект есть», когда в той же схеме уже работал другой
    каталог или конфиг указывает на другой сервер. Ничего не создаёт.

    Raises:
        DatabaseError: нет DSN/пароля, DSN не разбирается или сервер недоступен.
    """
    target = database_target(settings)
    marker = settings.data_dir / "postgresql-project.json"
    try:
        if json.loads(marker.read_text(encoding="utf-8")) != _project_identity(target):
            return False
    except (OSError, ValueError):
        return False
    return target_exists(target)

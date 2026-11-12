"""Общая инфраструктура команд `mnogobase`: корневое Typer-приложение, консоль, загрузка
настроек, предварительные проверки сервисов и блокировки писателя.

Все модули команд обращаются к функциям этого модуля через объект модуля
(`common.build_app(...)`, `common.console`), а не импортируют имена к себе. Так у тестов одна
точка подмены: `monkeypatch.setattr(common, "build_app", fake)` действует на все команды сразу.

Соглашение о кодах выхода для всех команд:
- 0 — команда выполнена полностью;
- 1 — команда отработала, но часть работы не удалась (упавшие файлы или страницы wiki,
  неполный прогон оценки) либо пользователь отказался от подтверждения;
- 2 — известная ошибка, которую исправляет пользователь: битый конфиг, вызов вне проекта,
  недоступный сервис, несовместимый индекс, занятая блокировка, ошибка PostgreSQL.
  Сообщение печатается без traceback (`fail`, `DatabaseSafeGroup`).

Блокировки писателя — advisory-блокировки PostgreSQL (`PgLock`), а не файлы: они общие для
CLI на хосте и контейнеров Docker, которые смотрят в одну базу.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import httpx
import openai
import typer
import yaml
from rich.console import Console
from rich.markup import escape
from sqlalchemy.exc import DBAPIError
from typer.core import TyperGroup

from mnogobase.app import App, build_app
from mnogobase.core.config import Settings, load_settings
from mnogobase.core.database import (
    DatabaseError,
    LockBusyError,
    PgLock,
    database_lock,
    database_target,
    project_exists,
)
from mnogobase.core.log import configure_logging, get_logger, safe_error
from mnogobase.ingest.pipeline import EmbedderMismatchError
from mnogobase.llm.client import StructuredOutputError
from mnogobase.ops.doctor import run_checks
from mnogobase.ops.graph_analysis import AnalysisError
from mnogobase.retrieval import build_retrievers
from mnogobase.retrieval.answer import Answerer
from mnogobase.retrieval.base import Retriever
from mnogobase.retrieval.translate import QueryTranslator
from mnogobase.stores.qdrant_store import DimensionMismatchError
from mnogobase.stores.registry import ingest_lock

# Имена, которые тесты подменяют на этом модуле; команды вызывают их только как `common.X`.
__all__ = ["build_app", "configure_logging", "load_settings", "run_checks"]

# Сервисы, без которых команда не может начать работу: эмбеддер, LLM, Qdrant, Neo4j.
PREFLIGHT_CHECKS = ("models", "llm", "qdrant", "neo4j")
INGEST_CHECKS = (*PREFLIGHT_CHECKS, "tokenizer")  # только ingest режет документы на чанки
REINDEX_CHECKS = ("models", "qdrant", "neo4j")  # reindex не вызывает LLM
RESET_CHECKS = ("qdrant", "neo4j")  # reset только удаляет данные

console = Console()
# Глобальный параметр `--config` из корневого callback; читается при загрузке настроек.
state: dict[str, Path | str | None] = {"config": None}


class DatabaseSafeGroup(TyperGroup):
    """Корневая группа команд: ошибки PostgreSQL превращаются в понятное сообщение и
    код выхода 2 вместо traceback с DSN или SQL."""

    def invoke(self, ctx):
        """Запускает выбранную команду и перехватывает ошибки PostgreSQL из любой из них.

        `DatabaseError` уже несёт безопасный текст и печатается как есть. Сырой `DBAPIError`
        от SQLAlchemy может содержать SQL, параметры и адрес сервера, поэтому его текст
        заменяется общим сообщением."""
        try:
            return super().invoke(ctx)
        except DatabaseError as exc:
            fail(exc)
        except DBAPIError:
            fail(DatabaseError("Database operation failed; check PostgreSQL and schema version"))


app = typer.Typer(
    cls=DatabaseSafeGroup,
    no_args_is_help=True,
    help="mnogobase — LLM Wiki core: ingest, wiki, retrieval.",
)


@app.callback()
def main(
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Path to config.yaml.")
    ] = None,
) -> None:
    """Корневой callback: запоминает глобальный `--config` до запуска подкоманды.

    Настройки здесь не загружаются: их читает сама команда (`get_settings`), поэтому
    `--help` и команды, которым конфиг не нужен, работают без config.yaml."""
    state["config"] = config


def get_settings(*, project: bool = False, missing_exit: int = 2) -> Settings:
    """Загружает настройки (config.yaml, `.env`, переменные `MNOGOBASE_*`) и включает
    логирование в файл.

    `project=True` сначала требует существующий проект (иначе выход с кодом
    `missing_exit`), чтобы команда, запущенная не в том каталоге, не создала ни таблиц
    проекта в PostgreSQL, ни каталога logs/. Отсутствующий или невалидный конфиг — выход
    с кодом 2 и коротким сообщением."""
    try:
        settings = load_settings(state["config"])
    except FileNotFoundError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2) from None
    except (yaml.YAMLError, ValueError) as exc:  # битый YAML или ValidationError от pydantic
        console.print(f"[red]Invalid configuration:[/red] {escape(str(exc))}")
        raise typer.Exit(2) from None
    if project:
        require_project(settings, missing_exit)
    configure_logging(settings.logs_dir)
    return settings


def preflight(
    settings: Settings, checks: tuple[str, ...] = PREFLIGHT_CHECKS, *, check_dim: bool = True
) -> None:
    """Завершает команду заранее (код 2), если недоступен нужный ей сервис или, при
    `check_dim`, размерность коллекций Qdrant не совпадает с эмбеддером.

    `checks` — имена проверок `doctor` (см. `PREFLIGHT_CHECKS` и соседние наборы). Проверка
    до начала работы дешевле, чем упасть посреди ingest с полузаписанными стадиями. Каждая
    проверка делает сетевой запрос к своему сервису."""
    failed = [c for c in run_checks(settings, only=checks, check_dim=check_dim) if not c.ok]
    if failed:
        for check in failed:
            console.print(f"[red]✗ {check.name}[/red]: {escape(check.detail)}")
        console.print("Run `mnogobase doctor` for a full report.")
        raise typer.Exit(2)


def require_project(settings: Settings, code: int = 2) -> None:
    """Выход с `code` вне проекта: команды, которые только читают, не должны создавать
    SQL-таблицы проекта.

    Проект существует, если есть маркер `.mnogobase/postgresql-project.json` и таблица
    `files` в схеме проекта (`project_exists`)."""
    if not project_exists(settings):
        console.print(
            f"No mnogobase project here (no {escape(str(database_target(settings)))}). "
            "Run `mnogobase ingest` first, or pass the project's --config.",
            soft_wrap=True,
        )
        raise typer.Exit(code)


def corpus_lock(settings: Settings) -> PgLock:
    """Берёт блокировку писателя корпуса (`ingest`). Второй писатель (ещё один ingest,
    remove, wiki build, reindex, reset или worker) получает понятный отказ и код 2.
    Вызывающий обязан освободить блокировку (`lock.release()` в `finally`).

    Блокировка не ждёт: занята — сразу отказ. Держит её отдельное соединение PostgreSQL,
    поэтому она снимается и при аварийном завершении процесса."""
    lock = ingest_lock(database_target(settings))
    try:
        lock.acquire()
    except LockBusyError:
        console.print(
            "[red]Another ingest / remove / wiki / reindex / reset run or worker is in progress."
            "[/red]"
        )
        raise typer.Exit(2) from None
    return lock


def fail(exc: Exception) -> None:
    """Известная, исправимая пользователем ошибка: печатает сообщение и выходит с кодом 2
    без traceback."""
    console.print(f"[red]{escape(str(exc))}[/red]")
    raise typer.Exit(2) from exc


@contextmanager
def graph_writer(settings: Settings) -> Iterator[None]:
    """Блокировки для записи аналитики графа: сначала глобальная `graph-analysis`, затем
    блокировка корпуса. Порядок graph-analysis → worker → corpus общий для всего проекта;
    обратный порядок где-либо ещё привёл бы к взаимной блокировке."""
    try:
        with database_lock(settings, "graph-analysis"):
            lock = corpus_lock(settings)
            try:
                yield
            finally:
                lock.release()
    except LockBusyError:
        fail(AnalysisError("Another graph analysis is in progress"))


def answer_setup(
    application: App,
) -> tuple[dict[str, Retriever], Answerer, QueryTranslator | None]:
    """Готовит всё для ответа на вопрос: проверяет индекс (подпись эмбеддера и размерность),
    строит retriever'ы всех режимов, генератор ответа и, если включён, переводчик вопроса.

    Возвращает `(retrievers, answerer, translator)`: `retrievers` — словарь
    `{режим: Retriever}`, `translator` — `None` при `retrieval.translate: false`. Если индекс
    построен другим эмбеддером, команда завершается с кодом 2 и советом сделать reindex.
    Блокировок не берёт: ответ только читает базу."""
    try:
        application.pipeline.prepare(resume=False)
    except (EmbedderMismatchError, DimensionMismatchError) as exc:
        fail(exc)
    s = application.settings
    retrievers = build_retrievers(
        application.vectors,
        application.graph,
        application.embedder,
        application.sparse,
        s,
        application.reranker,
    )
    answerer = Answerer(application.llm, s.retrieval.context_tokens, s.retrieval.answer_language)
    translator = QueryTranslator(application.llm) if s.retrieval.translate else None
    return retrievers, answerer, translator


@contextmanager
def model_errors() -> Iterator[None]:
    """Сбой LLM или эмбеддера во время ответа: короткое сообщение и код 2 вместо traceback."""
    try:
        yield
    except (openai.OpenAIError, httpx.HTTPError, StructuredOutputError) as exc:
        get_logger(__name__).error("answer_failed", error=safe_error(exc))
        fail(RuntimeError(f"Model call failed: {safe_error(exc)}"))

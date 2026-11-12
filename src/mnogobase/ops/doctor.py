"""Проверки окружения: `mnogobase doctor` и предварительные проверки команд.

`run_checks` по очереди проверяет то, без чего mnogobase не работает: устройство,
PostgreSQL, серверы моделей (embedder, reranker, VLM), токенизатор чанкера, LLM, Qdrant,
Neo4j и его плагины, совместимость индекса с текущим embedder и смену типов сущностей.
`mnogobase doctor` печатает все проверки; остальные команды через `cli.common.preflight`
запускают только нужное им подмножество и завершаются с кодом 2, если что-то не так, —
это дешевле, чем упасть посреди ingest с полузаписанными стадиями.

Проверки только читают: каждая делает сетевой запрос к своему сервису, а таблицы
registry открываются, лишь если проект уже существует (`target_exists`), так что doctor
вне проекта ничего не создаёт. Исключение в проверке не роняет doctor: оно превращается в
неуспешный `Check` с текстом ошибки.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass

import httpx
from tenacity import wait_none

from mnogobase.chunking.tokenizer import AUTO, load_tokenizer, tokenizer_source
from mnogobase.core.config import ServedModel, Settings
from mnogobase.core.database import (
    connect,
    database_target,
    target_exists,
)
from mnogobase.core.device import detect_device
from mnogobase.core.http import auth_headers
from mnogobase.embedding.base import embedder_signature, signature_mismatch
from mnogobase.embedding.openai_compat import OpenAICompatEmbedder
from mnogobase.extraction.extractor import TYPES_CHANGED, stale_entity_types
from mnogobase.stores.graph_store import GraphStore
from mnogobase.stores.qdrant_store import QdrantStore, connect_qdrant
from mnogobase.stores.registry import Registry

# все проверки в порядке выполнения; `run_checks(only=...)` выбирает подмножество
CHECKS: tuple[str, ...] = (
    "device",
    "database",
    "models",
    "tokenizer",
    "llm",
    "qdrant",
    "neo4j",
    "graph_plugins",
    "index",
    "types",
)


@dataclass
class Check:
    """Результат одной проверки: имя из `CHECKS`, успех, текст для человека и флаг
    предупреждения."""

    name: str
    ok: bool
    detail: str
    warn: bool = False  # стоит внимания, но не ошибка (doctor всё равно выходит с 0)


def _served_models(base_url: str, api_key_env: str | None, timeout: float) -> set[str]:
    """Имена моделей из `GET {base_url}/models` (у llama.cpp server это `--alias`)."""
    resp = httpx.get(f"{base_url}/models", timeout=timeout, headers=auth_headers(api_key_env))
    resp.raise_for_status()
    data = resp.json().get("data", [])
    return {m["id"] for m in data} | {a for m in data for a in m.get("aliases") or []}


def _guard(name: str, fn: Callable[[], Check]) -> Check:
    """Выполняет проверку; любое исключение превращает в неуспешный `Check` с его текстом."""
    try:
        return fn()
    except Exception as exc:  # doctor должен сообщать, а не падать
        return Check(name, False, f"{type(exc).__name__}: {exc}")


def run_checks(
    settings: Settings,
    timeout: float = 5.0,
    only: Collection[str] | None = None,
    *,
    check_dim: bool = True,
) -> list[Check]:
    """Запускает проверки окружения в порядке `CHECKS` (только перечисленные в `only`, если
    он задан).

    `check_dim=False` допускает коллекции с размерностью, отличной от конфига: это нужно для
    `reindex`, который удаляет и пересоздаёт их. `timeout` (секунды) — на каждый сетевой
    запрос. Возвращает список `Check` (не бросает: ошибки внутри проверок становятся
    неуспешными `Check`). Размерность embedder запрашивается у сервера не больше одного
    раза на вызов."""
    # один embedder на все проверки: при `dim: auto` размерность — один пробный запрос;
    # без повторов с ожиданием (`wait_none`), чтобы doctor не висел на недоступном сервере
    embed_client = httpx.Client(
        base_url=settings.embedder.url,
        timeout=timeout,
        headers=auth_headers(settings.embedder.api_key_env),
    )
    embedder = OpenAICompatEmbedder(settings.embedder, client=embed_client, retry_wait=wait_none())
    # первая ошибка запроса размерности: дальше сервер не дёргаем, а `index` её показывает
    unreachable: list[str] = []

    def embedder_dim() -> int | None:
        """Размерность векторов эмбеддера; None, если для неё нужен сервер, а он не ответил
        (это сообщает проверка models)."""
        if unreachable:
            return None
        try:
            return embedder.dim
        except httpx.HTTPError as exc:
            unreachable.append(f"{type(exc).__name__}: {exc}")
            return None

    def device() -> Check:
        """Устройство по настройке `device` (mps или cpu; `auto` выбирает само)."""
        return Check("device", True, detect_device(settings.device))

    def database() -> Check:
        """PostgreSQL отвечает на `SELECT 1`; таблиц не создаёт."""
        target = database_target(settings)
        connection = connect(target)
        try:
            connection.execute("SELECT 1")
        finally:
            connection.close()
        return Check("database", True, str(target))

    def models() -> Check:
        """Каждый сервер embedder / reranker / VLM отдаёт нужную модель в `/models`.

        Роли группируются по (url, ключ), чтобы общий сервер опрашивался один раз.
        Недоступный локальный сервер (`serve: llamacpp`) — понятный совет `models up`;
        недоступный внешний (`serve: api`) — исключение, которое `_guard` покажет как есть."""
        # embedder, reranker и VLM могут работать на разных серверах
        roles: list[tuple[str, ServedModel]] = [("embedder", settings.embedder)]
        if settings.rerank.enabled:
            roles.append(("reranker", settings.rerank))
        if settings.vision.enabled:
            roles.append(("vision", settings.vision))
        servers: dict[tuple[str, str | None], list[ServedModel]] = {}
        for _, s in roles:
            servers.setdefault((s.url, s.api_key_env), []).append(s)
        problems = []
        for (url, key_env), wanted in servers.items():
            try:
                served = _served_models(url, key_env, timeout)
            except httpx.TransportError as exc:
                if all(s.serve == "api" for s in wanted):
                    raise
                problems.append(
                    f"{url} is not reachable ({type(exc).__name__}): "
                    "start the local models with `mnogobase models up`"
                )
                continue
            for model in (s.model for s in wanted):
                if model not in served:
                    listed = ", ".join(sorted(served)) or "nothing"
                    problems.append(f"{model} is not served by {url} (it serves {listed})")
        if problems:
            return Check("models", False, "; ".join(problems))
        return Check("models", True, ", ".join(f"{r} {s.model}" for r, s in roles) + " available")

    def tokenizer() -> Check:
        """Токенизатор чанкера загружается и считает токены (при `auto` — `/tokenize`
        сервера embedder)."""
        tok = load_tokenizer(settings.chunking, settings.embedder)
        tok.count_tokens("Hello, мир")
        prefix = "auto: " if settings.chunking.tokenizer == AUTO else ""
        return Check("tokenizer", True, prefix + tokenizer_source(tok))

    def llm() -> Check:
        """LLM-сервер принимает ключ и (если отдаёт список моделей) обслуживает `llm.model`.

        Пустой список моделей считается успехом: не все совместимые API его заполняют."""
        resp = httpx.get(
            f"{settings.llm.url}/models",
            timeout=timeout,
            headers={"Authorization": f"Bearer {settings.llm.api_key()}"},
        )
        where = f"{settings.llm.model} @ {settings.llm.url}"
        if resp.status_code in (404, 405):
            # некоторые OpenAI-совместимые endpoint'ы отдают chat completions без списка
            # моделей; 401/403 (неверный ключ) и прочие ошибки всё равно падают ниже
            return Check("llm", True, f"{where} (models endpoint not available)")
        resp.raise_for_status()
        ids = {m["id"] for m in resp.json().get("data", [])}
        if not ids or settings.llm.model in ids:
            return Check("llm", True, where)
        return Check("llm", False, f"{settings.llm.model} is not served by {settings.llm.url}")

    def qdrant() -> Check:
        """Qdrant доступен, и размерность коллекции чанков совпадает с embedder.

        Нет коллекции — успех ("no index yet"). При `check_dim=False` несовпадение только
        упоминается, потому что `reindex` пересоздаст коллекции."""
        # проверка сообщает о доступности; warning клиента о проверке версии пропускаем
        client = connect_qdrant(settings.qdrant, timeout=int(timeout), check_compatibility=False)
        try:
            store = QdrantStore(client, settings.qdrant.prefix, lambda: embedder.dim)
            dim = store.collection_dim(store.chunks)
        finally:
            client.close()
        if dim is None:
            return Check("qdrant", True, f"{settings.qdrant.url} (no index yet)")
        expected = embedder_dim()
        if expected is None:
            detail = f"dim {dim}, not compared: the embedder is not reachable"
            return Check("qdrant", True, f"{settings.qdrant.url} ({detail})")
        if dim != expected:
            source = "embedder" if settings.embedder.dim == "auto" else "config"
            mismatch = f"{store.chunks} has dim {dim}, {source} {expected}"
            if check_dim:
                return Check("qdrant", False, f"{mismatch}: run `mnogobase reindex`")
            return Check("qdrant", True, f"{settings.qdrant.url} ({mismatch}: rebuilt)")
        return Check("qdrant", True, f"{settings.qdrant.url} (dim {dim})")

    def neo4j() -> Check:
        """Neo4j принимает подключение с настроенными учётными данными."""
        graph = GraphStore.from_settings(settings.neo4j)
        try:
            graph.verify()
        finally:
            graph.close()
        return Check("neo4j", True, settings.neo4j.uri)

    def graph_plugins() -> Check:
        """Версии APOC и GDS и отсутствующие процедуры; отсутствие — только
        предупреждение."""
        graph = GraphStore.from_settings(settings.neo4j)
        try:
            status = graph.plugin_status()
        finally:
            graph.close()
        detail = f"APOC {status['apoc'] or 'unavailable'}, GDS {status['gds'] or 'unavailable'}"
        if status["missing"]:
            detail += "; missing: " + ", ".join(status["missing"])
        # Плагины не нужны обычным ingest / rag / wiki; `graph analyze` проверяет их строго.
        return Check("graph_plugins", True, detail, warn=bool(status["missing"]))

    def index() -> Check:
        """Сигнатура embedder в registry совпадает с текущей (иначе нужен `reindex`).

        Вне проекта — успех; если размерность нельзя узнать (embedder недоступен) —
        неуспех с причиной."""
        db = database_target(settings)
        if not target_exists(db):
            return Check("index", True, "no index yet")
        if embedder_dim() is None:
            return Check("index", False, f"embedder dimension unknown: {unreachable[0]}")
        expected = embedder_signature(embedder)
        registry = Registry(db)
        try:
            stored = registry.get_meta("embedder")
        finally:
            registry.close()
        mismatch = signature_mismatch(stored, embedder)
        if mismatch:
            return Check("index", False, f"{mismatch}: run `mnogobase reindex`")
        return Check("index", True, f"embedder {expected}")

    def types() -> Check:
        """Предупреждение, если часть корпуса извлечена с другим набором
        `extract.entity_types`."""
        db = database_target(settings)
        if not target_exists(db):
            return Check("types", True, "no index yet")
        registry = Registry(db)
        try:
            stale = stale_entity_types(registry, settings.extract.entity_types)
        finally:
            registry.close()
        if stale:
            return Check("types", True, TYPES_CHANGED, warn=True)
        return Check("types", True, f"{len(settings.extract.entity_types)} entity types")

    checks = {
        "device": device,
        "database": database,
        "models": models,
        "tokenizer": tokenizer,
        "llm": llm,
        "qdrant": qdrant,
        "neo4j": neo4j,
        "graph_plugins": graph_plugins,
        "index": index,
        "types": types,
    }
    try:
        return [_guard(name, checks[name]) for name in CHECKS if only is None or name in only]
    finally:
        embed_client.close()

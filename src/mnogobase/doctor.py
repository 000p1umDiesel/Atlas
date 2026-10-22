from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass

import httpx
from qdrant_client import QdrantClient

from mnogobase.chunking.tokenizer import AUTO, load_tokenizer, tokenizer_source
from mnogobase.config import Settings
from mnogobase.device import detect_device
from mnogobase.embedding.base import embedder_signature, signature_mismatch
from mnogobase.embedding.http import auth_headers
from mnogobase.embedding.openai_compat import OpenAICompatEmbedder
from mnogobase.extraction.extractor import TYPES_CHANGED, stale_entity_types
from mnogobase.registry import Registry
from mnogobase.stores.graph_store import GraphStore
from mnogobase.stores.qdrant_store import QdrantStore

CHECKS: tuple[str, ...] = (
    "device",
    "models",
    "tokenizer",
    "llm",
    "qdrant",
    "neo4j",
    "index",
    "types",
)


@dataclass
class Check:
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
    `reindex`, который удаляет и пересоздаёт их."""

    def device() -> Check:
        return Check("device", True, detect_device(settings.device))

    def models() -> Check:
        # embedder и reranker могут работать на разных серверах
        roles = [("embedder", settings.embedder)]
        if settings.rerank.enabled:
            roles.append(("reranker", settings.rerank))
        servers: dict[tuple[str, str | None], list[str]] = {}
        for _, s in roles:
            servers.setdefault((s.base_url.rstrip("/"), s.api_key_env), []).append(s.model)
        problems = []
        for (url, key_env), wanted in servers.items():
            served = _served_models(url, key_env, timeout)
            for model in wanted:
                if model not in served:
                    listed = ", ".join(sorted(served)) or "nothing"
                    problems.append(f"{model} is not served by {url} (it serves {listed})")
        if problems:
            return Check("models", False, "; ".join(problems))
        return Check("models", True, ", ".join(f"{r} {s.model}" for r, s in roles) + " available")

    def tokenizer() -> Check:
        tok = load_tokenizer(settings.chunking, settings.embedder)
        tok.count_tokens("Hello, мир")
        prefix = "auto: " if settings.chunking.tokenizer == AUTO else ""
        return Check("tokenizer", True, prefix + tokenizer_source(tok))

    def llm() -> Check:
        resp = httpx.get(
            f"{settings.llm.base_url.rstrip('/')}/models",
            timeout=timeout,
            headers={"Authorization": f"Bearer {settings.llm.api_key()}"},
        )
        where = f"{settings.llm.model} @ {settings.llm.base_url}"
        if resp.status_code in (404, 405):
            # некоторые OpenAI-совместимые endpoint'ы отдают chat completions без списка
            # моделей; 401/403 (неверный ключ) и прочие ошибки всё равно падают ниже
            return Check("llm", True, f"{where} (models endpoint not available)")
        resp.raise_for_status()
        ids = {m["id"] for m in resp.json().get("data", [])}
        if not ids or settings.llm.model in ids:
            return Check("llm", True, where)
        return Check("llm", False, f"{settings.llm.model} is not served by {settings.llm.base_url}")

    def qdrant() -> Check:
        # проверка сообщает о доступности; warning клиента о проверке версии пропускаем
        client = QdrantClient(
            url=settings.qdrant.url, timeout=int(timeout), check_compatibility=False
        )
        try:
            store = QdrantStore(client, settings.qdrant.prefix, settings.embedder.dim)
            dim = store.collection_dim(store.chunks)
        finally:
            client.close()
        if dim is not None and dim != settings.embedder.dim:
            mismatch = f"{store.chunks} has dim {dim}, config {settings.embedder.dim}"
            if check_dim:
                return Check("qdrant", False, f"{mismatch}: run `mnogobase reindex`")
            return Check("qdrant", True, f"{settings.qdrant.url} ({mismatch}: rebuilt)")
        state = "no index yet" if dim is None else f"dim {dim}"
        return Check("qdrant", True, f"{settings.qdrant.url} ({state})")

    def neo4j() -> Check:
        graph = GraphStore.from_settings(settings.neo4j)
        try:
            graph.verify()
        finally:
            graph.close()
        return Check("neo4j", True, settings.neo4j.uri)

    def index() -> Check:
        db = settings.data_dir / "state.db"
        embedder = OpenAICompatEmbedder(settings.embedder)
        expected = embedder_signature(embedder)
        if not db.exists():
            return Check("index", True, "no index yet")
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
        db = settings.data_dir / "state.db"
        if not db.exists():
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
        "models": models,
        "tokenizer": tokenizer,
        "llm": llm,
        "qdrant": qdrant,
        "neo4j": neo4j,
        "index": index,
        "types": types,
    }
    return [_guard(name, checks[name]) for name in CHECKS if only is None or name in only]

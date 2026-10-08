from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass

import httpx
from qdrant_client import QdrantClient

from mnogobase.config import Settings
from mnogobase.device import detect_device
from mnogobase.embedding.base import embedder_signature, signature_mismatch
from mnogobase.embedding.ollama import OllamaEmbedder
from mnogobase.extraction.extractor import TYPES_CHANGED, stale_entity_types
from mnogobase.registry import Registry
from mnogobase.stores.graph_store import GraphStore
from mnogobase.stores.qdrant_store import QdrantStore

CHECKS: tuple[str, ...] = ("device", "ollama", "llm", "qdrant", "neo4j", "index", "types")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    warn: bool = False  # worth attention, but not a failure (doctor still exits 0)


def _guard(name: str, fn: Callable[[], Check]) -> Check:
    try:
        return fn()
    except Exception as exc:  # doctor must report, never crash
        return Check(name, False, f"{type(exc).__name__}: {exc}")


def run_checks(
    settings: Settings,
    timeout: float = 5.0,
    only: Collection[str] | None = None,
    *,
    check_dim: bool = True,
) -> list[Check]:
    """Run the environment checks in `CHECKS` order (only those named in `only`, if given).

    `check_dim=False` accepts collections of another dimension than the config: for
    `reindex`, which drops and recreates them."""

    def device() -> Check:
        return Check("device", True, detect_device(settings.device))

    def ollama() -> Check:
        resp = httpx.get(f"{settings.embedder.base_url}/api/tags", timeout=timeout)
        resp.raise_for_status()
        names = {m["name"] for m in resp.json().get("models", [])}
        wanted = {"embedder": settings.embedder.model}
        # the reranker may be served by another Ollama; only a shared one is checked here
        same_host = settings.rerank.base_url.rstrip("/") == settings.embedder.base_url.rstrip("/")
        if settings.rerank.enabled and same_host:
            wanted["reranker"] = settings.rerank.model
        missing = [m for m in wanted.values() if m not in names and f"{m}:latest" not in names]
        if missing:
            pulls = "; ".join(f"`ollama pull {m}`" for m in missing)
            return Check("ollama", False, f"missing {', '.join(missing)}: run {pulls}")
        return Check(
            "ollama", True, ", ".join(f"{r} {m}" for r, m in wanted.items()) + " available"
        )

    def llm() -> Check:
        resp = httpx.get(
            f"{settings.llm.base_url.rstrip('/')}/models",
            timeout=timeout,
            headers={"Authorization": f"Bearer {settings.llm.api_key()}"},
        )
        where = f"{settings.llm.model} @ {settings.llm.base_url}"
        if resp.status_code in (404, 405):
            # some OpenAI-compatible endpoints serve chat completions without a model list;
            # 401/403 (bad key) and other errors still fail below
            return Check("llm", True, f"{where} (models endpoint not available)")
        resp.raise_for_status()
        ids = {m["id"] for m in resp.json().get("data", [])}
        if not ids or settings.llm.model in ids:
            return Check("llm", True, where)
        return Check("llm", False, f"{settings.llm.model} is not served by {settings.llm.base_url}")

    def qdrant() -> Check:
        # reachability is what this check reports; skip the client's own version probe warning
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
        embedder = OllamaEmbedder(settings.embedder)
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
        "ollama": ollama,
        "llm": llm,
        "qdrant": qdrant,
        "neo4j": neo4j,
        "index": index,
        "types": types,
    }
    return [_guard(name, checks[name]) for name in CHECKS if only is None or name in only]

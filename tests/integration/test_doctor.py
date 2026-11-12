import httpx
import pytest
from qdrant_client import QdrantClient

from mnogobase.chunking.tokenizer import TokenizerError
from mnogobase.core.config import EmbedderSettings, RerankSettings, Settings, VisionSettings
from mnogobase.core.database import database_target, target_exists
from mnogobase.embedding.base import embedder_signature
from mnogobase.embedding.openai_compat import OpenAICompatEmbedder
from mnogobase.extraction.extractor import PROMPT_VERSION, entity_types_signature
from mnogobase.ops import doctor
from mnogobase.ops.doctor import run_checks
from mnogobase.stores.qdrant_store import QdrantStore
from mnogobase.stores.registry import Registry

pytestmark = pytest.mark.integration


def test_plugin_check_reports_missing_optional_procedures(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from mnogobase.stores.graph_store import GraphStore

    store = SimpleNamespace(
        plugin_status=lambda: {"apoc": None, "gds": None, "missing": ["gds.leiden.stream"]},
        close=lambda: None,
    )
    monkeypatch.setattr(GraphStore, "from_settings", lambda *a: store)
    [check] = run_checks(make_settings(tmp_path), only=["graph_plugins"])
    assert check.ok and check.warn
    assert "gds.leiden.stream" in check.detail


def make_settings(tmp_path, **update) -> Settings:
    # dim задан явно: при `auto` проверки спросили бы его у сервера эмбеддера
    embedder = EmbedderSettings(dim=2560)
    return Settings().model_copy(
        update={"data_dir": tmp_path / ".mb", "embedder": embedder, **update}
    )


def mock_embedder(monkeypatch, handler) -> None:
    """Эмбеддер doctor отвечает через `handler` вместо сервера."""

    def build(settings, client, retry_wait):
        mock = httpx.Client(base_url=settings.url, transport=httpx.MockTransport(handler))
        return OpenAICompatEmbedder(settings, client=mock, retry_wait=retry_wait)

    monkeypatch.setattr(doctor, "OpenAICompatEmbedder", build)


def store_signature(tmp_path, signature: str) -> None:
    registry = Registry(database_target(Settings()))
    registry.set_meta("embedder", signature)
    registry.close()


def test_index_check_compares_with_the_configured_embedder(tmp_path):
    settings = make_settings(tmp_path)
    [fresh] = run_checks(settings, only=["index"])
    assert fresh.ok and fresh.detail == "индекса пока нет"
    assert not target_exists(database_target(Settings()))  # проверка ничего не создаёт

    store_signature(tmp_path, embedder_signature(OpenAICompatEmbedder(settings.embedder)))
    [same] = run_checks(settings, only=["index"])
    assert same.ok

    store_signature(tmp_path, "ollama:qwen3-embedding:4b:2560:tpl-dbe41d80")  # индекс до llama.cpp
    [changed] = run_checks(settings, only=["index"])
    assert not changed.ok
    assert "ollama:qwen3-embedding:4b:2560" in changed.detail
    assert "llamacpp:Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M:2560" in changed.detail
    assert "mnogobase reindex" in changed.detail


def test_index_check_fails_when_only_the_templates_changed(tmp_path):
    settings = make_settings(tmp_path)
    store_signature(tmp_path, embedder_signature(OpenAICompatEmbedder(settings.embedder)))
    [same] = run_checks(settings, only=["index"])
    assert same.ok, same.detail

    embedder = EmbedderSettings(dim=2560, doc_template="passage: {text}")
    edited = make_settings(tmp_path, embedder=embedder)
    [changed] = run_checks(edited, only=["index"])
    assert not changed.ok
    assert (
        "изменились шаблоны эмбеддера" in changed.detail and "mnogobase reindex" in changed.detail
    )


def test_unreachable_service_is_a_failed_check_not_a_crash(tmp_path, monkeypatch):
    # имитируем отказ в соединении, не завися от того, что слушает на локальных портах
    def refuse(url, **kwargs):
        raise httpx.ConnectError("connection refused", request=httpx.Request("GET", url))

    monkeypatch.setattr(doctor.httpx, "get", refuse)
    settings = make_settings(tmp_path)
    checks = run_checks(settings, timeout=1.0, only=["index", "models"])
    assert [c.name for c in checks] == ["models", "index"]  # порядок doctor, а не порядок запроса
    assert not checks[0].ok and "ConnectError" in checks[0].detail


def _served(monkeypatch, servers: dict[str, list[dict]]) -> list[str]:
    """`GET <base_url>/models` каждого сервера; ключ — base_url."""
    seen: list[str] = []

    def fake_get(url, **kwargs):
        seen.append(url)
        data = servers[url.removesuffix("/models")]
        return httpx.Response(200, json={"data": data}, request=httpx.Request("GET", url))

    monkeypatch.setattr(doctor.httpx, "get", fake_get)
    return seen


def test_models_check_requires_the_reranker_only_when_enabled(tmp_path, monkeypatch):
    # один сервер для обеих ролей; у llama.cpp имя модели — её --alias
    url = "http://gpu:8001/v1"
    _served(monkeypatch, {url: [{"id": "/models/emb.gguf", "aliases": ["emb"]}]})
    embedder = EmbedderSettings(base_url=url + "/", model="emb")
    [plain] = run_checks(make_settings(tmp_path, embedder=embedder), only=["models"])
    assert plain.ok and plain.detail == "доступны: embedder emb"

    rerank = RerankSettings(enabled=True, base_url=url, model="rr")
    [missing] = run_checks(
        make_settings(tmp_path, embedder=embedder, rerank=rerank), only=["models"]
    )
    assert not missing.ok
    assert missing.detail == f"{url} не отдаёт модель rr (список моделей: /models/emb.gguf, emb)"


def test_models_check_asks_each_server(tmp_path, monkeypatch):
    seen = _served(
        monkeypatch,
        {"http://a:8001/v1": [{"id": "emb"}], "http://b:8002/v1": [{"id": "rr"}]},
    )
    embedder = EmbedderSettings(base_url="http://a:8001/v1", model="emb")
    rerank = RerankSettings(enabled=True, base_url="http://b:8002/v1", model="rr")
    [ok] = run_checks(make_settings(tmp_path, embedder=embedder, rerank=rerank), only=["models"])
    assert ok.ok and ok.detail == "доступны: embedder emb, reranker rr"
    assert seen == ["http://a:8001/v1/models", "http://b:8002/v1/models"]


def test_models_check_includes_the_vlm_when_enabled(tmp_path, monkeypatch):
    _served(monkeypatch, {"http://a:8001/v1": [{"id": "emb"}], "http://v:8003/v1": []})
    embedder = EmbedderSettings(base_url="http://a:8001/v1", model="emb")
    vision = VisionSettings(enabled=True, base_url="http://v:8003/v1", model="vl")
    [missing] = run_checks(
        make_settings(tmp_path, embedder=embedder, vision=vision), only=["models"]
    )
    assert (
        not missing.ok
        and missing.detail == "http://v:8003/v1 не отдаёт модель vl (список моделей: пуст)"
    )


def _models_endpoint(monkeypatch, status: int, payload: dict | None = None) -> list[str]:
    seen: list[str] = []

    def fake_get(url, **kwargs):
        seen.append(url)
        return httpx.Response(status, json=payload or {}, request=httpx.Request("GET", url))

    monkeypatch.setattr(doctor.httpx, "get", fake_get)
    return seen


@pytest.mark.parametrize("status", [404, 405])
def test_llm_without_models_endpoint_is_ok(tmp_path, monkeypatch, status):
    seen = _models_endpoint(monkeypatch, status)
    [check] = run_checks(make_settings(tmp_path), only=["llm"])
    assert seen == ["http://localhost:8000/v1/models"]
    assert check.ok, check.detail
    assert "эндпоинт /models недоступен" in check.detail


@pytest.mark.parametrize("status", [401, 403, 500])
def test_llm_auth_or_server_error_fails(tmp_path, monkeypatch, status):
    _models_endpoint(monkeypatch, status)
    [check] = run_checks(make_settings(tmp_path), only=["llm"])
    assert not check.ok and str(status) in check.detail


def test_llm_model_listed_or_empty_list_is_ok(tmp_path, monkeypatch):
    _models_endpoint(monkeypatch, 200, {"data": [{"id": "local"}]})
    assert run_checks(make_settings(tmp_path), only=["llm"])[0].ok
    _models_endpoint(monkeypatch, 200, {"data": []})
    assert run_checks(make_settings(tmp_path), only=["llm"])[0].ok


def test_types_check_warns_without_failing_when_the_types_changed(tmp_path):
    settings = make_settings(tmp_path)
    [fresh] = run_checks(settings, only=["types"])
    assert fresh.ok and not fresh.warn
    assert not target_exists(database_target(Settings()))  # проверка ничего не создаёт

    registry = Registry(database_target(Settings()))
    sig = entity_types_signature(settings.extract.entity_types)
    registry.put_extraction("c1", f"{PROMPT_VERSION}:{sig}", "m", "{}")
    [same] = run_checks(settings, only=["types"])
    assert same.ok and not same.warn

    registry.put_extraction("c2", f"{PROMPT_VERSION}:0123456789ab", "m", "{}")
    registry.close()
    [changed] = run_checks(settings, only=["types"])
    assert changed.ok and changed.warn  # предупреждение, но никогда не ошибка
    assert "mnogobase reset" in changed.detail and "ingest" in changed.detail


@pytest.mark.filterwarnings("ignore:Payload indexes have no effect in the local Qdrant:UserWarning")
def test_qdrant_dimension_mismatch_fails_unless_the_command_rebuilds_the_index(
    tmp_path, monkeypatch
):
    client = QdrantClient(":memory:")
    QdrantStore(client, "mb_", 64).ensure_collections()
    monkeypatch.setattr(client, "close", lambda: None)  # run_checks закрывает свой клиент
    monkeypatch.setattr(doctor, "connect_qdrant", lambda *args, **kwargs: client)
    settings = make_settings(tmp_path)  # embedder.dim 2560

    [strict] = run_checks(settings, only=["qdrant"])
    assert not strict.ok
    assert (
        "у mb_chunks размерность 64, а в конфиге 2560" in strict.detail
        and "reindex" in strict.detail
    )

    [lenient] = run_checks(settings, only=["qdrant"], check_dim=False)  # то, что запускает reindex
    assert lenient.ok, lenient.detail
    assert "размерность 64" in lenient.detail and "2560" in lenient.detail


@pytest.mark.filterwarnings("ignore:Payload indexes have no effect in the local Qdrant:UserWarning")
def test_auto_dimension_is_compared_with_what_the_embedder_returns(tmp_path, monkeypatch):
    client = QdrantClient(":memory:")
    QdrantStore(client, "mb_", 64).ensure_collections()
    monkeypatch.setattr(client, "close", lambda: None)
    monkeypatch.setattr(doctor, "connect_qdrant", lambda *args, **kwargs: client)
    settings = make_settings(tmp_path, embedder=EmbedderSettings())  # dim: auto
    store_signature(tmp_path, "llamacpp:Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M:64:tpl-dbe41d80")
    calls = []

    def vectors(size):
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0] * size}]})

        return handler

    mock_embedder(monkeypatch, vectors(64))
    qdrant, index = run_checks(settings, only=["qdrant", "index"])
    assert qdrant.ok and "размерность 64" in qdrant.detail
    assert index.ok and ":64:tpl-" in index.detail
    assert len(calls) == 1  # один пробный запрос на все проверки

    mock_embedder(monkeypatch, vectors(32))
    [smaller] = run_checks(settings, only=["qdrant"])
    assert not smaller.ok and "у mb_chunks размерность 64, а эмбеддер отдаёт 32" in smaller.detail

    def down(request):
        raise httpx.ConnectError("connection refused")

    mock_embedder(monkeypatch, down)
    qdrant, index = run_checks(settings, only=["qdrant", "index"])
    assert qdrant.ok and "эмбеддер недоступен" in qdrant.detail  # о самом сервере сообщает models
    assert not index.ok and "ConnectError" in index.detail


def test_tokenizer_check_reports_the_source_or_why_it_failed(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)

    class Tok:
        def count_tokens(self, text):
            return 3

    monkeypatch.setattr(doctor, "load_tokenizer", lambda chunking, embedder: Tok())
    monkeypatch.setattr(doctor, "tokenizer_source", lambda tok: "http://localhost:8001/tokenize")
    [ok] = run_checks(settings, only=["tokenizer"])
    assert ok.ok and ok.detail == "auto: http://localhost:8001/tokenize"

    def unavailable(chunking, embedder):
        raise TokenizerError("no tokenizer: set chunking.tokenizer")

    monkeypatch.setattr(doctor, "load_tokenizer", unavailable)
    [failed] = run_checks(settings, only=["tokenizer"])
    assert not failed.ok and "chunking.tokenizer" in failed.detail

import httpx
import pytest
from qdrant_client import QdrantClient

from mnogobase import doctor
from mnogobase.chunking.tokenizer import TokenizerError
from mnogobase.config import EmbedderSettings, RerankSettings, Settings
from mnogobase.doctor import run_checks
from mnogobase.embedding.base import embedder_signature
from mnogobase.embedding.openai_compat import OpenAICompatEmbedder
from mnogobase.extraction.extractor import PROMPT_VERSION, entity_types_signature
from mnogobase.registry import Registry
from mnogobase.stores.qdrant_store import QdrantStore


def make_settings(tmp_path, **update) -> Settings:
    return Settings().model_copy(update={"data_dir": tmp_path / ".mb", **update})


def store_signature(tmp_path, signature: str) -> None:
    registry = Registry(tmp_path / ".mb" / "state.db")
    registry.set_meta("embedder", signature)
    registry.close()


def test_index_check_compares_with_the_configured_embedder(tmp_path):
    settings = make_settings(tmp_path)
    [fresh] = run_checks(settings, only=["index"])
    assert fresh.ok and fresh.detail == "no index yet"
    assert not (tmp_path / ".mb" / "state.db").exists()

    store_signature(tmp_path, "openai:qwen3-embedding-4b:2560")  # старый формат: без шаблонов
    [same] = run_checks(settings, only=["index"])
    assert same.ok

    store_signature(tmp_path, "ollama:qwen3-embedding:4b:2560:tpl-dbe41d80")  # индекс до llama.cpp
    [changed] = run_checks(settings, only=["index"])
    assert not changed.ok
    assert "ollama:qwen3-embedding:4b:2560" in changed.detail
    assert "openai:qwen3-embedding-4b:2560" in changed.detail
    assert "mnogobase reindex" in changed.detail


def test_index_check_fails_when_only_the_templates_changed(tmp_path):
    settings = make_settings(tmp_path)
    store_signature(tmp_path, embedder_signature(OpenAICompatEmbedder(settings.embedder)))
    [same] = run_checks(settings, only=["index"])
    assert same.ok, same.detail

    edited = make_settings(tmp_path, embedder=EmbedderSettings(doc_template="passage: {text}"))
    [changed] = run_checks(edited, only=["index"])
    assert not changed.ok
    assert "templates changed" in changed.detail and "mnogobase reindex" in changed.detail


def test_unreachable_service_is_a_failed_check_not_a_crash(tmp_path, monkeypatch):
    # имитируем отказ в соединении: на Windows закрытый порт может вместо этого дать таймаут
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
    assert plain.ok and plain.detail == "embedder emb available"

    rerank = RerankSettings(enabled=True, base_url=url, model="rr")
    [missing] = run_checks(
        make_settings(tmp_path, embedder=embedder, rerank=rerank), only=["models"]
    )
    assert not missing.ok
    assert missing.detail == f"rr is not served by {url} (it serves /models/emb.gguf, emb)"


def test_models_check_asks_each_server(tmp_path, monkeypatch):
    seen = _served(
        monkeypatch,
        {"http://a:8001/v1": [{"id": "emb"}], "http://b:8002/v1": [{"id": "rr"}]},
    )
    embedder = EmbedderSettings(base_url="http://a:8001/v1", model="emb")
    rerank = RerankSettings(enabled=True, base_url="http://b:8002/v1", model="rr")
    [ok] = run_checks(make_settings(tmp_path, embedder=embedder, rerank=rerank), only=["models"])
    assert ok.ok and ok.detail == "embedder emb, reranker rr available"
    assert seen == ["http://a:8001/v1/models", "http://b:8002/v1/models"]


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
    assert seen == ["https://codex.sale/v1/models"]
    assert check.ok, check.detail
    assert "models endpoint not available" in check.detail


@pytest.mark.parametrize("status", [401, 403, 500])
def test_llm_auth_or_server_error_fails(tmp_path, monkeypatch, status):
    _models_endpoint(monkeypatch, status)
    [check] = run_checks(make_settings(tmp_path), only=["llm"])
    assert not check.ok and str(status) in check.detail


def test_llm_model_listed_or_empty_list_is_ok(tmp_path, monkeypatch):
    _models_endpoint(monkeypatch, 200, {"data": [{"id": "gpt-6-luna"}]})
    assert run_checks(make_settings(tmp_path), only=["llm"])[0].ok
    _models_endpoint(monkeypatch, 200, {"data": []})
    assert run_checks(make_settings(tmp_path), only=["llm"])[0].ok


def test_types_check_warns_without_failing_when_the_types_changed(tmp_path):
    settings = make_settings(tmp_path)
    [fresh] = run_checks(settings, only=["types"])
    assert fresh.ok and not fresh.warn
    assert not (tmp_path / ".mb" / "state.db").exists()

    registry = Registry(tmp_path / ".mb" / "state.db")
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
    monkeypatch.setattr(doctor, "QdrantClient", lambda **kwargs: client)
    settings = make_settings(tmp_path)  # embedder.dim 2560

    [strict] = run_checks(settings, only=["qdrant"])
    assert not strict.ok
    assert "mb_chunks has dim 64, config 2560" in strict.detail and "reindex" in strict.detail

    [lenient] = run_checks(settings, only=["qdrant"], check_dim=False)  # то, что запускает reindex
    assert lenient.ok, lenient.detail
    assert "dim 64" in lenient.detail and "2560" in lenient.detail


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

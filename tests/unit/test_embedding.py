import json
from pathlib import Path

import httpx
import pytest
from tenacity import wait_none

from mnogobase.config import EmbedderSettings
from mnogobase.embedding.base import (
    embedder_signature,
    format_document,
    format_query,
    signature_mismatch,
    truncate_normalize,
)
from mnogobase.embedding.openai_compat import OpenAICompatEmbedder
from mnogobase.models import EmbedInput
from tests.fakes import FakeEmbedder


def test_templates():
    s = EmbedderSettings(doc_template="title: {title} | text: {text}", query_template="q: {query}")
    assert (
        format_document(EmbedInput(text="hello", title="Doc"), s.doc_template)
        == "title: Doc | text: hello"
    )
    assert format_document(EmbedInput(text="hello"), s.doc_template) == "title: none | text: hello"
    assert format_document(EmbedInput(text="a {b}"), "{text}") == "a {b}"
    assert format_query("q?", s.query_template) == "q: q?"
    assert format_query("q?", EmbedderSettings().query_template).endswith("\nQuery: q?")
    with pytest.raises(NotImplementedError):
        format_document(EmbedInput(modality="image", path=Path("a.png")), s.doc_template)


def test_truncate_normalize():
    assert truncate_normalize([3.0, 4.0, 12.0], 2) == pytest.approx([0.6, 0.8])
    with pytest.raises(ValueError):
        truncate_normalize([1.0], 2)


def _client(handler, base_url="http://llama:8001/v1"):
    return httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler))


def _data(vectors):
    return {"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]}


def test_embedder_batches_and_applies_templates():
    bodies = []

    def handler(request):
        assert str(request.url) == "http://llama:8001/v1/embeddings"
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(200, json=_data([[1.0, 0.0, 0.0]] * len(body["input"])))

    settings = EmbedderSettings(dim=2, batch_size=2, query_template="q: {query}")
    emb = OpenAICompatEmbedder(settings, client=_client(handler))
    out = emb.embed_documents([EmbedInput(text=f"t{i}") for i in range(5)])
    assert len(out) == 5 and len(out[0]) == 2
    assert [len(b["input"]) for b in bodies] == [2, 2, 1]
    assert bodies[0] == {"model": "Qwen/Qwen3-Embedding-4B", "input": ["t0", "t1"]}
    emb.embed_query("hi")
    assert bodies[-1]["input"] == ["q: hi"]
    assert embedder_signature(emb).startswith("llamacpp:Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M:2:tpl-")


def test_embedder_orders_by_index_and_sends_the_api_key(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        data = [{"index": 1, "embedding": [0.0, 2.0]}, {"index": 0, "embedding": [3.0, 0.0]}]
        return httpx.Response(200, json={"data": data})

    monkeypatch.setenv("EMB_KEY", "secret")
    settings = EmbedderSettings(api_key_env="EMB_KEY", dim=2)
    emb = OpenAICompatEmbedder(settings)
    emb._client._transport = httpx.MockTransport(handler)  # заголовки клиента из настроек
    vectors = emb.embed_documents([EmbedInput(text="a"), EmbedInput(text="b")])
    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    assert str(seen[0].url) == "http://localhost:8001/v1/embeddings"
    assert seen[0].headers["authorization"] == "Bearer secret"


def test_signature_covers_the_templates_and_accepts_legacy_signatures():
    def sig(**update):
        return embedder_signature(OpenAICompatEmbedder(EmbedderSettings(**update)))

    default = sig()
    assert sig() == default  # стабильна
    assert sig(doc_template="title: {title} | {text}") != default
    assert sig(query_template="{query}") != default
    emb = OpenAICompatEmbedder(EmbedderSettings())
    for accepted in (None, default, "llamacpp:Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M:2560"):
        assert signature_mismatch(accepted, emb) is None, accepted
    templates = signature_mismatch(sig(doc_template="title: {title} | {text}"), emb)
    assert templates is not None and "templates changed" in templates
    model = signature_mismatch("ollama:qwen3-embedding:4b:2560:tpl-dbe41d80", emb)
    assert model is not None and "templates" not in model
    assert "ollama:qwen3-embedding:4b:2560" in model and default in model


def test_embedder_retries_server_errors():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"error": "loading"})
        return httpx.Response(200, json=_data([[0.0, 1.0]]))

    emb = OpenAICompatEmbedder(
        EmbedderSettings(dim=2), client=_client(handler), retry_wait=wait_none()
    )
    assert emb.embed_query("x") == [0.0, 1.0]
    assert calls["n"] == 2


def test_embedder_rejects_too_small_vectors():
    handler = lambda request: httpx.Response(200, json=_data([[1.0]]))  # noqa: E731
    emb = OpenAICompatEmbedder(EmbedderSettings(dim=2), client=_client(handler))
    with pytest.raises(ValueError, match="dims"):
        emb.embed_query("x")


def test_fake_embedder_similarity():
    emb = FakeEmbedder()
    a, b, c = emb.embed_documents(
        [
            EmbedInput(text=t)
            for t in ("transformer attention", "attention transformer model", "cats")
        ]
    )
    dot = lambda x, y: sum(p * q for p, q in zip(x, y, strict=True))  # noqa: E731
    assert dot(a, b) > dot(a, c)


def test_signature_follows_the_backend_and_the_quant():
    def model_id(**update):
        return OpenAICompatEmbedder(EmbedderSettings(**update)).model_id

    assert model_id() == "llamacpp:Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M"
    assert model_id(gguf="Qwen/Qwen3-Embedding-4B-GGUF:Q8_0") != model_id()
    assert model_id(serve="vllm") == "vllm:Qwen/Qwen3-Embedding-4B"
    assert model_id(serve="api", base_url="http://x/v1") == "api:Qwen/Qwen3-Embedding-4B"

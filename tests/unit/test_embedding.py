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
from mnogobase.embedding.ollama import OllamaEmbedder
from mnogobase.models import EmbedInput
from tests.fakes import FakeEmbedder


def test_templates():
    s = EmbedderSettings()
    assert (
        format_document(EmbedInput(text="hello", title="Doc"), s.doc_template)
        == "title: Doc | text: hello"
    )
    assert format_document(EmbedInput(text="hello"), s.doc_template) == "title: none | text: hello"
    assert format_document(EmbedInput(text="a {b}"), "{text}") == "a {b}"
    assert format_query("q?", s.query_template) == "task: search result | query: q?"
    with pytest.raises(NotImplementedError):
        format_document(EmbedInput(modality="image", path=Path("a.png")), s.doc_template)


def test_truncate_normalize():
    assert truncate_normalize([3.0, 4.0, 12.0], 2) == pytest.approx([0.6, 0.8])
    with pytest.raises(ValueError):
        truncate_normalize([1.0], 2)


def _client(handler):
    return httpx.Client(base_url="http://ollama", transport=httpx.MockTransport(handler))


def test_ollama_batches_and_applies_templates():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(200, json={"embeddings": [[1.0, 0.0, 0.0]] * len(body["input"])})

    emb = OllamaEmbedder(EmbedderSettings(dim=2, batch_size=2), client=_client(handler))
    out = emb.embed_documents([EmbedInput(text=f"t{i}") for i in range(5)])
    assert len(out) == 5 and len(out[0]) == 2
    assert [len(b["input"]) for b in bodies] == [2, 2, 1]
    assert bodies[0]["input"][0] == "title: none | text: t0"
    assert bodies[0]["model"] == "embeddinggemma-2:740m"
    emb.embed_query("hi")
    assert bodies[-1]["input"] == ["task: search result | query: hi"]
    assert embedder_signature(emb) == "ollama:embeddinggemma-2:740m:2:tpl-dd6dd8c7"


def test_signature_covers_the_templates_and_accepts_legacy_signatures():
    def sig(**update):
        return embedder_signature(OllamaEmbedder(EmbedderSettings(**update)))

    default = sig()
    assert sig() == default  # stable
    assert sig(doc_template="{text}") != default
    assert sig(query_template="{query}") != default
    emb = OllamaEmbedder(EmbedderSettings())
    for accepted in (None, default, "ollama:embeddinggemma-2:740m:768"):
        assert signature_mismatch(accepted, emb) is None, accepted
    templates = signature_mismatch(sig(doc_template="{text}"), emb)
    assert templates is not None and "templates changed" in templates
    model = signature_mismatch("ollama:qwen3-embedding:0.6b:1024", emb)
    assert model is not None and "templates" not in model
    assert "ollama:qwen3-embedding:0.6b:1024" in model and default in model


def test_ollama_num_ctx_is_sent_only_when_set():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"embeddings": [[1.0, 0.0]]})

    OllamaEmbedder(EmbedderSettings(dim=2), client=_client(handler)).embed_query("x")
    OllamaEmbedder(EmbedderSettings(dim=2, num_ctx=2048), client=_client(handler)).embed_query("x")
    assert "options" not in bodies[0]
    assert bodies[1]["options"] == {"num_ctx": 2048}


def test_ollama_retries_server_errors():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"error": "loading"})
        return httpx.Response(200, json={"embeddings": [[0.0, 1.0]]})

    emb = OllamaEmbedder(EmbedderSettings(dim=2), client=_client(handler), retry_wait=wait_none())
    assert emb.embed_query("x") == [0.0, 1.0]
    assert calls["n"] == 2


def test_ollama_rejects_too_small_vectors():
    handler = lambda request: httpx.Response(200, json={"embeddings": [[1.0]]})  # noqa: E731
    emb = OllamaEmbedder(EmbedderSettings(dim=2), client=_client(handler))
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

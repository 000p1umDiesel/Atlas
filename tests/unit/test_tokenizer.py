import json

import httpx
import pytest

from mnogobase.chunking import tokenizer as tokenizer_module
from mnogobase.chunking.tokenizer import (
    ServerTokenizer,
    TokenizerError,
    load_tokenizer,
    tokenizer_source,
)
from mnogobase.config import ChunkingSettings, EmbedderSettings

AUTO = ChunkingSettings(max_tokens=64)
EMBEDDER = EmbedderSettings(model="emb")


def _server(models: list[dict], seen: list | None = None, tokenize_status: int = 200):
    """Корень сервера: `/v1/models` и `/tokenize`, токен — слово."""

    def handler(request):
        if seen is not None:
            seen.append((request.url.path, request.content))
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": models})
        if request.url.path == "/tokenize" and tokenize_status == 200:
            text = json.loads(request.content)["content"]
            return httpx.Response(200, json={"tokens": list(range(len(text.split())))})
        return httpx.Response(tokenize_status or 404)

    return httpx.Client(base_url="http://llama:8001", transport=httpx.MockTransport(handler))


def test_explicit_repo_skips_the_server(monkeypatch):
    loaded: list = []
    monkeypatch.setattr(
        tokenizer_module, "_from_hf", lambda repo, max_tokens: loaded.append((repo, max_tokens))
    )
    settings = ChunkingSettings(tokenizer="Qwen/Qwen3-Embedding-4B", max_tokens=128)
    load_tokenizer(settings, EMBEDDER, client=_server([]))
    assert loaded == [("Qwen/Qwen3-Embedding-4B", 128)]


def test_auto_on_vllm_loads_the_repo_it_reports(monkeypatch):
    loaded: list = []
    monkeypatch.setattr(
        tokenizer_module, "_from_hf", lambda repo, max_tokens: loaded.append(repo) or "tok"
    )
    client = _server([{"id": "emb", "root": "Qwen/Qwen3-Embedding-4B"}])
    assert load_tokenizer(AUTO, EMBEDDER, client=client) == "tok"
    assert loaded == ["Qwen/Qwen3-Embedding-4B"]


def test_auto_on_llama_cpp_counts_with_its_tokenize_and_caches():
    seen: list = []
    client = _server([{"id": "emb", "aliases": ["emb"]}], seen)
    tok = load_tokenizer(AUTO, EMBEDDER, client=client)
    assert isinstance(tok, ServerTokenizer) and tok.get_max_tokens() == 64
    assert tok.count_tokens("one two three") == 3
    assert tok.count_tokens("one two three") == 3
    tokenize = [json.loads(body) for path, body in seen if path == "/tokenize"]
    # пробный запрос при загрузке и один на текст: повтор берётся из кэша
    assert tokenize == [
        {"content": "tokenizer probe", "add_special": False},
        {"content": "one two three", "add_special": False},
    ]
    assert tokenizer_source(tok) == "http://llama:8001/tokenize"


@pytest.mark.parametrize(
    ("models", "status", "message"),
    [
        ([{"id": "emb"}], 404, "no /tokenize"),  # сервер без репозитория и без /tokenize
        ([{"id": "emb", "root": "/models/qwen"}], 200, "local path"),  # vLLM с локальной моделью
        ([{"id": "other", "root": "Qwen/Other"}], 200, "not served"),
    ],
)
def test_auto_without_a_way_to_tokenize_points_to_the_config(models, status, message):
    with pytest.raises(TokenizerError, match=message):
        load_tokenizer(AUTO, EMBEDDER, client=_server(models, tokenize_status=status))


def test_unknown_repo_is_a_tokenizer_error(monkeypatch):
    from transformers import AutoTokenizer

    def missing(repo):
        raise OSError(f"{repo} is not a valid model identifier")

    monkeypatch.setattr(AutoTokenizer, "from_pretrained", missing)
    with pytest.raises(TokenizerError, match="cannot load the tokenizer 'nope/nope'"):
        load_tokenizer(ChunkingSettings(tokenizer="nope/nope"), EMBEDDER)

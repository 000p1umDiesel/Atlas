import json
import sys

import httpx
import pytest
from tokenizers import Tokenizer, models, pre_tokenizers
from transformers.convert_slow_tokenizer import bytes_to_unicode

from mnogobase.chunking.tokenizer import TokenizerError, _manifest_path, load_tokenizer
from mnogobase.config import EmbedderSettings

EMBEDDER = EmbedderSettings(model="tiny:1b")


def _client(model_info: dict, seen: list | None = None) -> httpx.Client:
    def handler(request):
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(200, json={"model_info": model_info})

    return httpx.Client(base_url="http://ollama", transport=httpx.MockTransport(handler))


def _gguf_info(tokenizer_type: str = "gpt2") -> dict:
    # байтовый алфавит GPT-2, два слияния и управляющий токен, как в Qwen3
    tokens = [*bytes_to_unicode().values(), "he", "the", "<|endoftext|>"]
    return {
        "general.architecture": "qwen3",
        "tokenizer.ggml.model": tokenizer_type,
        "tokenizer.ggml.pre": "qwen2",
        "tokenizer.ggml.tokens": tokens,
        "tokenizer.ggml.token_type": [1] * (len(tokens) - 1) + [3],
        "tokenizer.ggml.merges": ["h e", "t he"],
        "tokenizer.ggml.eos_token_id": len(tokens) - 1,
    }


@pytest.mark.parametrize(
    ("model", "parts"),
    [
        ("qwen3-embedding:4b", ("registry.ollama.ai", "library", "qwen3-embedding", "4b")),
        ("nomic-embed-text", ("registry.ollama.ai", "library", "nomic-embed-text", "latest")),
        (
            "dengcao/Qwen3-Reranker-4B:Q4_K_M",
            ("registry.ollama.ai", "dengcao", "Qwen3-Reranker-4B", "Q4_K_M"),
        ),
        ("hf.co/org/repo:Q8_0", ("hf.co", "org", "repo", "Q8_0")),
        ("host:5000/org/repo", ("host:5000", "org", "repo", "latest")),
    ],
)
def test_manifest_path_follows_the_ollama_layout(tmp_path, model, parts):
    assert _manifest_path(tmp_path, model) == tmp_path.joinpath("manifests", *parts)


def test_auto_rebuilds_the_tokenizer_from_gguf_metadata(tmp_path):
    seen: list = []
    tok = load_tokenizer("auto", EMBEDDER, client=_client(_gguf_info(), seen), models_dir=tmp_path)
    assert seen == [{"model": "tiny:1b", "verbose": True}]
    assert tok.tokenize("the hehe") == ["the", "Ġ", "he", "he"]
    assert tok.tokenize("the<|endoftext|>") == ["the", "<|endoftext|>"]  # спецтокен не дробится


def test_auto_rejects_a_tokenizer_type_it_cannot_rebuild(tmp_path):
    client = _client(_gguf_info(tokenizer_type="mystery"))
    with pytest.raises(TokenizerError, match="chunking.tokenizer"):
        load_tokenizer("auto", EMBEDDER, client=client, models_dir=tmp_path)


def test_auto_reads_tokenizer_json_of_a_safetensors_model(tmp_path):
    backend = Tokenizer(models.WordLevel({"[UNK]": 0, "hello": 1, "world": 2}, unk_token="[UNK]"))
    backend.pre_tokenizer = pre_tokenizers.Whitespace()
    (tmp_path / "blobs").mkdir()
    backend.save(str(tmp_path / "blobs" / "sha256-abc"))
    manifest = _manifest_path(tmp_path, "tiny:1b")
    manifest.parent.mkdir(parents=True)
    layers = [
        {"mediaType": "application/vnd.ollama.image.json", "digest": "sha256-x", "name": "a.json"},
        {
            "mediaType": "application/vnd.ollama.image.json",
            "digest": "sha256:abc",
            "name": "tokenizer.json",
        },
    ]
    manifest.write_text(json.dumps({"layers": layers}), encoding="utf-8")
    tok = load_tokenizer(
        "auto", EMBEDDER, client=_client({"general.architecture": "x"}), models_dir=tmp_path
    )
    assert tok.tokenize("hello world") == ["hello", "world"]


def test_auto_without_any_tokenizer_points_to_the_config(tmp_path):
    client = _client({"general.architecture": "x"})
    with pytest.raises(TokenizerError, match="OLLAMA_MODELS.*chunking.tokenizer"):
        load_tokenizer("auto", EMBEDDER, client=client, models_dir=tmp_path)


def test_auto_explains_when_transformers_moved_the_gguf_converter(tmp_path, monkeypatch):
    # None в sys.modules заставляет import бросить ImportError, как после переезда модуля
    monkeypatch.setitem(sys.modules, "transformers.integrations.gguf.gguf_tokenizer_mapping", None)
    with pytest.raises(TokenizerError, match="transformers version.*chunking.tokenizer"):
        load_tokenizer("auto", EMBEDDER, client=_client(_gguf_info()), models_dir=tmp_path)


def test_auto_explains_when_the_gguf_converter_changed_shape(tmp_path, monkeypatch):
    from transformers.integrations.gguf import gguf_tokenizer_mapping

    monkeypatch.setattr(gguf_tokenizer_mapping, "GGUF_TOKENIZER_MAPPING", {"renamed": {}})
    with pytest.raises(TokenizerError, match="cannot rebuild.*chunking.tokenizer"):
        load_tokenizer("auto", EMBEDDER, client=_client(_gguf_info()), models_dir=tmp_path)

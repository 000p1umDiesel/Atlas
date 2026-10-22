from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from mnogobase.config import EmbedderSettings
from mnogobase.embedding.http import auth_headers

if TYPE_CHECKING:
    from transformers import PreTrainedTokenizerBase

AUTO = "auto"
_HF_HINT = "set chunking.tokenizer to the model's Hugging Face repo"


class TokenizerError(RuntimeError):
    """Токенизатор модели эмбеддера не удаётся получить из Ollama."""


def load_tokenizer(
    name: str,
    embedder: EmbedderSettings,
    *,
    client: httpx.Client | None = None,
    models_dir: Path | None = None,
) -> PreTrainedTokenizerBase:
    """Токенизатор для подсчёта токенов чанка: HF-репозиторий `name` или, при `auto`, токенизатор
    самой модели эмбеддера, чтобы он всегда соответствовал `embedder.model`: из Ollama или, у
    OpenAI-совместимого сервера, по HF-репозиторию, который он сообщает (vLLM)."""
    if name != AUTO:
        return _from_hf(name)
    if client is None:
        headers = auth_headers(embedder.api_key_env)
        with httpx.Client(base_url=embedder.base_url, timeout=60, headers=headers) as own:
            return load_tokenizer(name, embedder, client=own, models_dir=models_dir)
    if embedder.provider == "openai":
        return _from_hf(_served_repo(embedder.model, client))
    return _from_ollama(embedder.model, client, models_dir or _models_dir())


def _from_hf(repo: str) -> PreTrainedTokenizerBase:
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(repo)


def _served_repo(model: str, client: httpx.Client) -> str:
    """HF-репозиторий модели `model` по `/models` сервера: vLLM пишет его в `root`."""
    response = client.get("/models")
    response.raise_for_status()
    for entry in response.json().get("data", []):
        if model in (entry.get("id"), *(entry.get("aliases") or [])):
            root = entry.get("root")
            # llama.cpp server `root` не отдаёт, а vLLM с локальной моделью отдаёт путь на сервере
            if root and not root.startswith(("/", ".")):
                return root
            raise TokenizerError(
                f"the server does not report the Hugging Face repo of {model} "
                f"(root: {root!r}): {_HF_HINT}"
            )
    raise TokenizerError(f"{model} is not served by {client.base_url}: check embedder.model")


def _from_ollama(model: str, client: httpx.Client, models_dir: Path) -> PreTrainedTokenizerBase:
    from transformers import PreTrainedTokenizerFast

    response = client.post("/api/show", json={"model": model, "verbose": True})
    response.raise_for_status()
    info = response.json().get("model_info") or {}
    # GGUF-модели отдают словарь и merges в метаданных; safetensors-модели (новый движок Ollama)
    # их не отдают, но хранят tokenizer.json отдельным слоем в локальном хранилище
    if "tokenizer.ggml.tokens" in info:
        return PreTrainedTokenizerFast(tokenizer_object=_gguf_backend(model, info))
    path = _tokenizer_file(model, models_dir)
    if path is None:
        raise TokenizerError(
            f"Ollama exposes no tokenizer for {model} and its tokenizer.json is not under "
            f"{models_dir} (Ollama on another host? set OLLAMA_MODELS): {_HF_HINT}"
        )
    return PreTrainedTokenizerFast(tokenizer_file=str(path))


def _gguf_backend(model: str, info: dict):
    # внутренний модуль transformers (тот же код, что у from_pretrained(gguf_file=...)); если он
    # переедет при обновлении, это поймает tests/unit/test_tokenizer.py, а здесь будет понятная
    # ошибка вместо traceback
    try:
        from transformers.integrations.gguf.gguf_tokenizer_mapping import (
            GGUF_TOKENIZER_MAPPING,
            convert_gguf_tokenizer,
        )
    except ImportError as exc:  # в т.ч. ModuleNotFoundError и отсутствующее имя
        raise TokenizerError(
            f"this transformers version cannot rebuild the tokenizer of {model} from GGUF "
            f"({exc}); {_HF_HINT}"
        ) from exc

    try:
        fields = {
            name: info[f"tokenizer.{key}"]
            for key, name in GGUF_TOKENIZER_MAPPING["tokenizer"].items()
            if f"tokenizer.{key}" in info
        }
        backend, _ = convert_gguf_tokenizer(info.get("general.architecture", ""), fields)
    # ValueError — неподдерживаемый тип токенайзера, KeyError/TypeError — сменилась форма API
    except (ValueError, KeyError, TypeError) as exc:
        raise TokenizerError(f"cannot rebuild the tokenizer of {model}: {exc}; {_HF_HINT}") from exc
    return backend


def _models_dir() -> Path:
    return Path(os.environ.get("OLLAMA_MODELS") or Path.home() / ".ollama" / "models")


def _manifest_path(models_dir: Path, model: str) -> Path:
    """`[host/][namespace/]name[:tag]` -> manifests/host/namespace/name/tag, как хранит Ollama."""
    repo, sep, tag = model.rpartition(":")
    if not sep or "/" in tag:  # без тега (двоеточие может быть только портом хоста)
        repo, tag = model, "latest"
    parts = repo.split("/")
    parts = ["registry.ollama.ai", "library"][: max(0, 3 - len(parts))] + parts
    return models_dir.joinpath("manifests", *parts, tag)


def _tokenizer_file(model: str, models_dir: Path) -> Path | None:
    manifest = _manifest_path(models_dir, model)
    if not manifest.is_file():
        return None
    for layer in json.loads(manifest.read_text(encoding="utf-8")).get("layers", []):
        if layer.get("name") == "tokenizer.json":
            blob = models_dir / "blobs" / layer["digest"].replace(":", "-")
            return blob if blob.is_file() else None
    return None

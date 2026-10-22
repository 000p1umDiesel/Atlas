from __future__ import annotations

from collections.abc import Callable

import httpx
from docling_core.transforms.chunker.tokenizer.base import BaseTokenizer
from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
from pydantic import ConfigDict, PrivateAttr

from mnogobase.config import ChunkingSettings, EmbedderSettings
from mnogobase.embedding.http import auth_headers, server_root

AUTO = "auto"
_HF_HINT = "set chunking.tokenizer to the model's Hugging Face repo"
_CACHE_SIZE = 50_000


class TokenizerError(RuntimeError):
    """Токенизатор модели эмбеддера не удаётся получить с её сервера."""


class ServerTokenizer(BaseTokenizer):
    """Подсчёт токенов через `POST /tokenize` llama.cpp server: работает только токенизатор,
    модель не прогоняется. HybridChunker считает одни и те же тексты много раз, поэтому
    ответы кэшируются."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    client: httpx.Client  # base_url — корень сервера, без /v1
    max_tokens: int
    _cache: dict[str, int] = PrivateAttr(default_factory=dict)

    def count_tokens(self, text: str) -> int:
        if text not in self._cache:
            if len(self._cache) >= _CACHE_SIZE:
                self._cache.clear()
            response = self.client.post("/tokenize", json={"content": text, "add_special": False})
            response.raise_for_status()
            self._cache[text] = len(response.json()["tokens"])
        return self._cache[text]

    def get_max_tokens(self) -> int:
        return self.max_tokens

    def get_tokenizer(self) -> Callable[[str], int]:
        # HybridChunker отдаёт его semchunk для дробления длинных текстов: тот принимает
        # HF-токенизатор или функцию подсчёта токенов
        return self.count_tokens


def load_tokenizer(
    settings: ChunkingSettings, embedder: EmbedderSettings, *, client: httpx.Client | None = None
) -> BaseTokenizer:
    """Токенизатор для подсчёта токенов чанка: HF-репозиторий `chunking.tokenizer` или, при
    `auto`, токенизатор самой модели эмбеддера, чтобы он всегда соответствовал
    `embedder.model`: по HF-репозиторию, который сообщает сервер (vLLM), или `/tokenize`
    сервера (llama.cpp). `client` — клиент корня сервера (тесты)."""
    if settings.tokenizer != AUTO:
        return _from_hf(settings.tokenizer, settings.max_tokens)
    if client is None:
        client = httpx.Client(
            base_url=server_root(embedder.base_url),
            timeout=60,
            headers=auth_headers(embedder.api_key_env),
        )
    repo = _served_repo(embedder.model, client)
    if repo:
        return _from_hf(repo, settings.max_tokens)
    tokenizer = ServerTokenizer(client=client, max_tokens=settings.max_tokens)
    try:
        tokenizer.count_tokens("tokenizer probe")
    except httpx.HTTPStatusError as exc:
        raise TokenizerError(
            f"{embedder.model} has no Hugging Face repo in /v1/models and its server has no "
            f"/tokenize (HTTP {exc.response.status_code}): {_HF_HINT}"
        ) from exc
    return tokenizer


def tokenizer_source(tokenizer: BaseTokenizer) -> str:
    """Откуда токенизатор, для `doctor`."""
    if isinstance(tokenizer, ServerTokenizer):
        return str(tokenizer.client.base_url.join("/tokenize"))
    return tokenizer.get_tokenizer().name_or_path


def _from_hf(repo: str, max_tokens: int) -> HuggingFaceTokenizer:
    from transformers import AutoTokenizer

    try:
        return HuggingFaceTokenizer(
            tokenizer=AutoTokenizer.from_pretrained(repo), max_tokens=max_tokens
        )
    except OSError as exc:  # нет такого репозитория, нет доступа или сети
        raise TokenizerError(f"cannot load the tokenizer {repo!r}: {exc}") from exc


def _served_repo(model: str, client: httpx.Client) -> str | None:
    """HF-репозиторий модели `model` по `/v1/models`: vLLM пишет его в `root`; у llama.cpp его
    нет (None)."""
    response = client.get("/v1/models")
    response.raise_for_status()
    for entry in response.json().get("data", []):
        if model in (entry.get("id"), *(entry.get("aliases") or [])):
            root = entry.get("root")
            if not root:
                return None
            # vLLM с локальной моделью отдаёт путь на сервере, а не репозиторий
            if root.startswith(("/", ".")):
                raise TokenizerError(f"{model} is served from a local path {root!r}: {_HF_HINT}")
            return root
    raise TokenizerError(f"{model} is not served by {client.base_url}: check embedder.model")

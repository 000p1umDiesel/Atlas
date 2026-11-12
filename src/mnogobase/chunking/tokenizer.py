"""Токенизатор для чанкинга: считает токены так же, как их посчитает модель эмбеддера.

`HybridChunker` (см. `chunking/hybrid.py`) решает, где резать документ, по числу токенов,
поэтому токенизатор должен совпадать с токенизатором эмбеддера, иначе чанк может оказаться
длиннее контекста модели. Источников два, выбор — `chunking.tokenizer`:

- `auto` (по умолчанию) — `ServerTokenizer`: `POST /tokenize` того же llama.cpp server, что
  отдаёт `/embeddings`. Токенизатор зашит в GGUF, так что он автоматически меняется вместе с
  моделью и не требует HF-репозитория.
- имя HF-репозитория — `HuggingFaceTokenizer` из transformers (`AutoTokenizer`); нужен для
  сервера эмбеддера без `/tokenize`.

Модуль ничего не делает при импорте: клиент и проверочный запрос создаются только в
`load_tokenizer`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
from docling_core.transforms.chunker.tokenizer.base import BaseTokenizer
from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
from pydantic import ConfigDict, PrivateAttr

from mnogobase.core.config import ChunkingSettings, EmbedderSettings
from mnogobase.core.http import DEFAULT_RETRY_WAIT, auth_headers, post_json_with_retry, server_root

AUTO = "auto"
_HF_HINT = "укажите в chunking.tokenizer репозиторий модели на Hugging Face"
# Предел кэша ServerTokenizer: при переполнении кэш просто очищается целиком, этого хватает,
# потому что повторные подсчёты идут в пределах одного документа.
_CACHE_SIZE = 50_000


class TokenizerError(RuntimeError):
    """Токенизатор модели эмбеддера не удаётся получить: у сервера нет `/tokenize` или
    HF-репозиторий из `chunking.tokenizer` не загружается."""


class ServerTokenizer(BaseTokenizer):
    """Подсчёт токенов через `POST /tokenize` llama.cpp server: работает только токенизатор,
    модель не прогоняется. HybridChunker считает одни и те же тексты много раз, поэтому
    ответы кэшируются (в памяти процесса, по точному тексту)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    client: httpx.Client  # base_url — корень сервера, без /v1
    max_tokens: int
    retry_wait: Any = DEFAULT_RETRY_WAIT  # пауза между повторами при сбоях сети и 5xx
    _cache: dict[str, int] = PrivateAttr(default_factory=dict)

    def count_tokens(self, text: str) -> int:
        """Число токенов `text` без специальных токенов (BOS/EOS), как их видит модель.

        Побочные эффекты: HTTP-запрос к серверу при промахе кэша (до трёх попыток при
        сетевых ошибках и 5xx). Исключения: `httpx.HTTPStatusError` при 4xx (например,
        404 у сервера без `/tokenize`) или после исчерпания повторов."""
        if text not in self._cache:
            if len(self._cache) >= _CACHE_SIZE:
                self._cache.clear()
            response = post_json_with_retry(
                self.client,
                "/tokenize",
                {"content": text, "add_special": False},
                attempts=3,
                wait=self.retry_wait,
            )
            self._cache[text] = len(response.json()["tokens"])
        return self._cache[text]

    def get_max_tokens(self) -> int:
        """Лимит токенов на чанк (`chunking.max_tokens`)."""
        return self.max_tokens

    def close(self) -> None:
        """Закрывает HTTP-клиент сервера эмбеддера."""
        self.client.close()

    def get_tokenizer(self) -> Callable[[str], int]:
        # HybridChunker отдаёт его semchunk для дробления длинных текстов: тот принимает
        # HF-токенизатор или функцию подсчёта токенов
        return self.count_tokens


def load_tokenizer(
    settings: ChunkingSettings, embedder: EmbedderSettings, *, client: httpx.Client | None = None
) -> BaseTokenizer:
    """Токенизатор для подсчёта токенов чанка: HF-репозиторий `chunking.tokenizer` или, при
    `auto`, токенизатор самой модели эмбеддера, чтобы он всегда соответствовал её смене.

    `auto` — `/tokenize` сервера эмбеддера (llama.cpp, в том числе внешнего с `serve: api`):
    токенизатор зашит в GGUF, HF-репозиторий исходной модели не нужен. Сервер без `/tokenize`
    требует явный `chunking.tokenizer`. `client` — клиент корня сервера эмбеддера (тесты).

    Побочные эффекты: при `auto` создаёт HTTP-клиент и сразу делает пробный `/tokenize`, чтобы
    отсутствие эндпоинта обнаружилось до чанкинга, а не посреди документа; для HF-репозитория
    может скачать токенизатор с Hugging Face Hub.

    Исключения: `TokenizerError`, если у сервера нет `/tokenize` (HTTP-ошибка статуса) или
    HF-репозиторий не загружается. Сетевые ошибки (`httpx.TransportError`) пробрасываются
    как есть."""
    if settings.tokenizer != AUTO:
        return _from_hf(settings.tokenizer, settings.max_tokens)
    if client is None:
        client = httpx.Client(
            base_url=server_root(embedder.url),
            timeout=embedder.timeout_s,
            headers=auth_headers(embedder.api_key_env),
        )
    tokenizer = ServerTokenizer(client=client, max_tokens=settings.max_tokens)
    try:
        tokenizer.count_tokens("tokenizer probe")
    except httpx.HTTPStatusError as exc:
        raise TokenizerError(
            f"у сервера {embedder.model} нет /tokenize "
            f"(HTTP {exc.response.status_code}): {_HF_HINT}"
        ) from exc
    return tokenizer


def tokenizer_source(tokenizer: BaseTokenizer) -> str:
    """Откуда токенизатор, для `doctor`: URL `/tokenize` сервера или имя HF-репозитория."""
    if isinstance(tokenizer, ServerTokenizer):
        return str(tokenizer.client.base_url.join("/tokenize"))
    return tokenizer.get_tokenizer().name_or_path


def _from_hf(repo: str, max_tokens: int) -> HuggingFaceTokenizer:
    """Загружает токенизатор из HF-репозитория `repo` (из кэша или с Hub).

    transformers импортируется здесь, а не в начале модуля: импорт тяжёлый и при
    `tokenizer: auto` не нужен."""
    from transformers import AutoTokenizer

    try:
        return HuggingFaceTokenizer(
            tokenizer=AutoTokenizer.from_pretrained(repo), max_tokens=max_tokens
        )
    except OSError as exc:  # нет такого репозитория, нет доступа или сети
        raise TokenizerError(f"не удалось загрузить токенизатор {repo!r}: {exc}") from exc

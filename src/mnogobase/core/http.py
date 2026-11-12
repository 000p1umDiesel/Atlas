"""Общие HTTP-хелперы для OpenAI-совместимых серверов (эмбеддер, реранкер, VLM, токенайзер).

Синхронные клиенты httpx эмбеддера (`embedding/openai_compat.py`), реранкера
(`retrieval/rerank.py`), токенизатора (`chunking/tokenizer.py`), описания картинок
(`parsing/docling_parser.py`), а также `doctor` и `models` (`ops/`) берут отсюда повторы
(`post_json_with_retry`), заголовок авторизации (`auth_headers`) и корень сервера
(`server_root`). LLM сюда не относится: у неё свой асинхронный клиент OpenAI SDK с
повторами в `llm/client.py`.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

# 1, 2, 4, ... секунд между попытками, не больше 20: сервер модели успевает догрузиться
DEFAULT_RETRY_WAIT = wait_exponential(multiplier=1, max=20)


def retryable(exc: BaseException) -> bool:
    """Стоит ли повторять запрос: сетевые сбои и таймауты (`TransportError`) и ответы 5xx.

    4xx не повторяются — это ошибка запроса (неверная модель, слишком длинный вход),
    и повтор дал бы тот же ответ."""
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500


def post_json_with_retry(
    client: httpx.Client,
    path: str,
    payload: dict[str, Any],
    *,
    attempts: int = 5,
    wait=DEFAULT_RETRY_WAIT,
) -> httpx.Response:
    """POST JSON; повторяет сетевые сбои и ответы 5xx, остальные ошибки HTTP — сразу.

    `path` — относительно `base_url` клиента. До `attempts` попыток с паузами `wait`.
    Возвращает успешный ответ.

    Raises:
        httpx.HTTPStatusError: ответ 4xx или 5xx после последней попытки.
        httpx.TransportError: сеть недоступна после последней попытки.
    """
    for attempt in Retrying(
        retry=retry_if_exception(retryable),
        stop=stop_after_attempt(attempts),
        wait=wait,
        reraise=True,
    ):
        with attempt:
            response = client.post(path, json=payload)
            response.raise_for_status()
    return response


def auth_headers(api_key_env: str | None) -> dict[str, str]:
    """Bearer-ключ из переменной `api_key_env`, если она задана и не пуста (`--api-key` у
    llama.cpp server)."""
    key = os.environ.get(api_key_env) if api_key_env else None
    return {"Authorization": f"Bearer {key}"} if key else {}


def server_root(base_url: str) -> str:
    """Корень сервера по `base_url` OpenAI-API: служебные эндпоинты вроде `/tokenize` и
    `/health` у llama.cpp лежат вне `/v1`."""
    return base_url.rstrip("/").removesuffix("/v1")

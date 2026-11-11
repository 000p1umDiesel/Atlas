"""Общие HTTP-хелперы для OpenAI-совместимых серверов (эмбеддер, реранкер, VLM, токенайзер)."""

from __future__ import annotations

import os
from typing import Any

import httpx
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

DEFAULT_RETRY_WAIT = wait_exponential(multiplier=1, max=20)


def retryable(exc: BaseException) -> bool:
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
    """POST JSON; повторяет сетевые сбои и ответы 5xx, остальные ошибки HTTP — сразу."""
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

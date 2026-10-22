from __future__ import annotations

import os

import httpx
from tenacity import wait_exponential

DEFAULT_RETRY_WAIT = wait_exponential(multiplier=1, max=20)


def retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500


def auth_headers(api_key_env: str | None) -> dict[str, str]:
    """Bearer-ключ из переменной `api_key_env`, если она задана и не пуста (`--api-key` у
    llama.cpp server и vLLM)."""
    key = os.environ.get(api_key_env) if api_key_env else None
    return {"Authorization": f"Bearer {key}"} if key else {}


def server_root(base_url: str) -> str:
    """Корень сервера по `base_url` OpenAI-API: служебные эндпоинты вроде `/tokenize` и
    `/health` у llama.cpp и vLLM лежат вне `/v1`."""
    return base_url.rstrip("/").removesuffix("/v1")

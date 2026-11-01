"""HTTP-клиент для OpenAI-совместимых API; серверы моделей запускаются отдельно."""

from __future__ import annotations

import math
import os

import httpx
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential


def add_local_no_proxy() -> None:
    for key in ("NO_PROXY", "no_proxy"):
        hosts = [x.strip() for x in os.environ.get(key, "").split(",") if x.strip()]
        os.environ[key] = ",".join(dict.fromkeys([*hosts, "localhost", "127.0.0.1", "::1"]))


def retryable(exc: BaseException) -> bool:
    return isinstance(exc, httpx.TransportError) or (
        isinstance(exc, httpx.HTTPStatusError)
        and (exc.response.status_code == 429 or exc.response.status_code >= 500)
    )


class ModelAPI:
    def __init__(
        self, base_url: str, model: str, key_env: str = "", timeout: float = 360, transport=None
    ):
        self.model = model
        headers = (
            {"Authorization": f"Bearer {os.environ[key_env]}"} if os.environ.get(key_env) else {}
        )
        self.http = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.http.close()

    def request(self, method: str, endpoint: str, **kwargs):
        for attempt in Retrying(
            retry=retry_if_exception(retryable),
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, max=10),
            reraise=True,
        ):
            with attempt:
                response = self.http.request(method, endpoint, **kwargs)
                response.raise_for_status()
        return response.json()

    def models(self):
        return self.request("GET", "models")

    def embed(self, texts: list[str], dim: int | None = None) -> list[list[float]]:
        if not texts:
            return []
        data = self.request("POST", "embeddings", json={"model": self.model, "input": texts})[
            "data"
        ]
        data = sorted(data, key=lambda item: item["index"])
        if [item["index"] for item in data] != list(range(len(texts))):
            raise ValueError("embedding API returned missing/duplicate indexes")
        result = []
        for item in data:
            vector = item["embedding"]
            if dim is not None and len(vector) < dim:
                raise ValueError(f"embedding dimension {len(vector)} < requested {dim}")
            vector = vector[:dim] if dim is not None else vector
            norm = math.sqrt(sum(x * x for x in vector))
            if not norm or not math.isfinite(norm):
                raise ValueError("embedding is zero or nonfinite")
            result.append([x / norm for x in vector])
        if len({len(v) for v in result}) != 1:
            raise ValueError("embedding dimensions differ within batch")
        return result

    def chat(self, messages: list[dict], **params) -> dict:
        return self.request(
            "POST", "chat/completions", json={"model": self.model, "messages": messages, **params}
        )

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        result = self.request(
            "POST",
            "rerank",
            json={
                "model": self.model,
                "query": query,
                "documents": [t[:4000] for t in documents],
                "top_n": len(documents),
            },
        )["results"]
        if sorted(item["index"] for item in result) != list(range(len(documents))):
            raise ValueError("reranker returned missing/duplicate indexes")
        scores = [0.0] * len(documents)
        for item in result:
            value = float(item["relevance_score"])
            if not math.isfinite(value):
                raise ValueError("nonfinite reranker score")
            scores[item["index"]] = value
        return scores

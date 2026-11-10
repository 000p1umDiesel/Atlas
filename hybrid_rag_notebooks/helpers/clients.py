"""Небольшой клиент к OpenAI-совместимым серверам (llama-server, vLLM, облачные API)."""

from __future__ import annotations

import math
import os

import httpx
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


class ModelAPI:
    """Один клиент на одну модель: /models, /embeddings, /rerank, /chat/completions."""

    def __init__(
        self, base_url: str, model: str, key_env: str = "", timeout: float = 360, transport=None
    ):
        self.model = model
        key = os.environ.get(key_env) if key_env else None
        self.http = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {key}"} if key else {},
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
        """Повторяем запрос при сетевых сбоях, 429 и 5xx; остальные ошибки сразу наверх."""
        for attempt in Retrying(
            retry=retry_if_exception(_retryable),
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, max=10),
            reraise=True,
        ):
            with attempt:
                response = self.http.request(method, endpoint, **kwargs)
                response.raise_for_status()
        return response.json()

    def require_model(self):
        """Проверяем, что сервер отдаёт модель с нашим именем (для llama-server это --alias)."""
        available = [m["id"] for m in self.request("GET", "models")["data"]]
        if self.model not in available:
            raise ValueError(f"{self.http.base_url}: нет модели {self.model!r}, есть {available}")

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        data = self.request("POST", "embeddings", json={"model": self.model, "input": texts})
        rows = sorted(data["data"], key=lambda item: item["index"])
        if len(rows) != len(texts):
            raise ValueError(f"ожидали {len(texts)} векторов, сервер вернул {len(rows)}")
        vectors = []
        for row in rows:
            vector = row["embedding"]
            norm = math.sqrt(sum(x * x for x in vector))
            if not norm or not math.isfinite(norm):
                raise ValueError("сервер вернул нулевой или битый вектор")
            vectors.append([x / norm for x in vector])
        return vectors

    def chat(self, messages: list[dict], **params) -> dict:
        body = {"model": self.model, "messages": messages, **params}
        return self.request("POST", "chat/completions", json=body)

    def rerank(self, query: str, documents: list[str], max_chars: int = 4000) -> list[float]:
        """Возвращает оценку для каждого документа в исходном порядке."""
        if not documents:
            return []
        body = {
            "model": self.model,
            "query": query,
            "documents": [text[:max_chars] for text in documents],
            "top_n": len(documents),
        }
        scores = [None] * len(documents)
        for item in self.request("POST", "rerank", json=body)["results"]:
            scores[item["index"]] = float(item["relevance_score"])
        if any(score is None for score in scores):
            raise ValueError("reranker вернул оценки не для всех документов")
        return scores

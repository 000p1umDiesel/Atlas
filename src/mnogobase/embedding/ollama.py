from __future__ import annotations

from mnogobase.embedding.http import HttpEmbedder


class OllamaEmbedder(HttpEmbedder):
    provider = "ollama"
    path = "/api/embed"

    def _payload(self, inputs: list[str]) -> dict:
        payload = {"model": self._s.model, "input": inputs, "truncate": True}
        if self._s.num_ctx:
            payload["options"] = {"num_ctx": self._s.num_ctx}
        return payload

    def _vectors(self, body: dict) -> list[list[float]]:
        return body["embeddings"]

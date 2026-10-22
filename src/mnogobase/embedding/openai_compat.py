from __future__ import annotations

from mnogobase.embedding.http import HttpEmbedder


class OpenAICompatEmbedder(HttpEmbedder):
    """`/embeddings` OpenAI-совместимого сервера: vLLM, llama.cpp server, LM Studio, TEI.

    `base_url` включает `/v1`. Контекст задаётся при запуске сервера (`num_ctx` не
    используется), длинный вход сервер не обрезает, а отклоняет: чанки не длиннее
    `chunking.max_tokens` должны помещаться в его окно."""

    provider = "openai"
    path = "/embeddings"

    def _payload(self, inputs: list[str]) -> dict:
        return {"model": self._s.model, "input": inputs}

    def _vectors(self, body: dict) -> list[list[float]]:
        return [item["embedding"] for item in sorted(body["data"], key=lambda d: d["index"])]

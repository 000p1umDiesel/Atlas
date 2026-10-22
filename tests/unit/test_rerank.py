import json
import math

import httpx
import pytest
from tenacity import wait_none

from mnogobase.config import RerankSettings
from mnogobase.retrieval.rag import RagRetriever
from mnogobase.retrieval.rerank import (
    ApiReranker,
    OllamaReranker,
    build_reranker,
    yes_probability,
)
from tests.fakes import FakeEmbedder, FakeSparse
from tests.unit.test_retrieval import StubChunks, _chunk


def lp(p: float) -> float:
    return math.log(p)


def test_yes_probability_sums_case_variants_and_normalizes():
    top = [
        {"token": "yes", "logprob": lp(0.5)},
        {"token": " Yes", "logprob": lp(0.1)},
        {"token": "no", "logprob": lp(0.3)},
        {"token": "maybe", "logprob": lp(0.1)},
    ]
    assert yes_probability(top) == pytest.approx(0.6 / 0.9)
    assert yes_probability([{"token": "the", "logprob": lp(0.9)}]) == 0.0


def _client(handler) -> httpx.Client:
    return httpx.Client(base_url="http://ollama", transport=httpx.MockTransport(handler))


def _reply(p_yes: float) -> httpx.Response:
    top = [{"token": "yes", "logprob": lp(p_yes)}, {"token": "no", "logprob": lp(1 - p_yes)}]
    return httpx.Response(200, json={"response": "yes", "logprobs": [{"top_logprobs": top}]})


def test_ollama_reranker_scores_each_document_in_input_order():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        return _reply(0.9 if "relevant" in body["prompt"] else 0.2)

    settings = RerankSettings(model="rr", max_chars=200, instruction="INSTR")
    reranker = OllamaReranker(settings, client=_client(handler))
    scores = reranker.score("Q?", ["noise", "relevant text", "x" * 1000])
    assert scores == pytest.approx([0.2, 0.9, 0.2])
    body = seen[0]
    assert body["model"] == "rr" and body["raw"] is True and body["logprobs"] is True
    assert body["options"]["num_predict"] == 1 and body["options"]["num_ctx"] == 4096
    assert "<Instruct>: INSTR\n<Query>: Q?\n<Document>: " in body["prompt"]
    assert body["prompt"].endswith("<think>\n\n</think>\n\n")
    assert max(len(b["prompt"]) for b in seen) < 200 + 600  # документ обрезан
    assert reranker.score("Q?", []) == []


def test_ollama_reranker_retries_and_requires_logprobs():
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] == 1 else _reply(0.7)

    reranker = OllamaReranker(RerankSettings(), client=_client(flaky), retry_wait=wait_none())
    assert reranker.score("q", ["d"]) == pytest.approx([0.7])

    bare = OllamaReranker(
        RerankSettings(), client=_client(lambda r: httpx.Response(200, json={"response": "yes"}))
    )
    with pytest.raises(RuntimeError, match="logprobs"):
        bare.score("q", ["d"])


class StubReranker:
    def __init__(self, scores: dict[str, float] | Exception):
        self.scores = scores
        self.calls: list[tuple[str, list[str]]] = []

    def score(self, query, documents):
        self.calls.append((query, list(documents)))
        if isinstance(self.scores, Exception):
            raise self.scores
        return [next((s for w, s in self.scores.items() if w in d), 0.0) for d in documents]


CHUNKS = dict(
    [
        _chunk("d:00000", "alpha", ["A"], 1),
        _chunk("d:00001", "beta", ["B"], 2),
        _chunk("d:00002", "gamma", ["C"], 3),
    ]
)
HITS = ["d:00000", "d:00001", "d:00002"]


def test_rag_reranks_candidates_and_keeps_top_k():
    reranker = StubReranker({"gamma": 0.9, "alpha": 0.5})
    rag = RagRetriever(
        StubChunks(CHUNKS, HITS), FakeEmbedder(), FakeSparse(), reranker=reranker, candidates=3
    )
    items = rag.retrieve("q", 2)
    assert [(i.ref, i.score) for i in items] == [("d:00002", 0.9), ("d:00000", 0.5)]
    # каждый кандидат оценён вместе с путём заголовков по исходному вопросу
    assert reranker.calls == [("q", ["A\nalpha", "B\nbeta", "C\ngamma"])]


def test_rag_keeps_hybrid_order_on_ties_and_when_the_reranker_fails():
    tied = RagRetriever(
        StubChunks(CHUNKS, HITS), FakeEmbedder(), FakeSparse(), reranker=StubReranker({})
    )
    assert [i.ref for i in tied.retrieve("q", 3)] == HITS
    broken = RagRetriever(
        StubChunks(CHUNKS, HITS),
        FakeEmbedder(),
        FakeSparse(),
        reranker=StubReranker(RuntimeError("ollama down")),
    )
    assert [i.ref for i in broken.retrieve("q", 2)] == HITS[:2]


def test_api_reranker_sends_raw_texts_once_and_maps_scores_back_to_input_order():
    seen = []

    def handler(request):
        seen.append(request)
        results = [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.2}]
        return httpx.Response(200, json={"results": results})

    settings = RerankSettings(
        provider="openai",
        base_url="http://vllm:8002/v1",
        model="Qwen/Qwen3-Reranker-4B",
        max_chars=100,
    )
    client = httpx.Client(base_url=settings.base_url, transport=httpx.MockTransport(handler))
    reranker = ApiReranker(settings, client=client)
    assert reranker.score("q", ["a" * 150, "b", "c"]) == [0.2, 0.9, 0.0]  # c не вернулся
    assert reranker.score("q", []) == []
    assert len(seen) == 1 and str(seen[0].url) == "http://vllm:8002/v1/rerank"
    body = json.loads(seen[0].content)
    assert body == {
        "model": "Qwen/Qwen3-Reranker-4B",
        "query": "q",
        "documents": ["a" * 100, "b", "c"],
        "top_n": 3,
    }


def test_build_reranker_follows_enabled_and_provider():
    assert build_reranker(RerankSettings()) is None
    assert isinstance(build_reranker(RerankSettings(enabled=True)), OllamaReranker)
    api = RerankSettings(enabled=True, provider="openai", base_url="http://x/v1")
    assert isinstance(build_reranker(api), ApiReranker)

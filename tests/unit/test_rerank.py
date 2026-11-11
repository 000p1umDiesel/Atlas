import json

import httpx
from tenacity import wait_none

from mnogobase.config import RerankSettings
from mnogobase.retrieval.rag import RagRetriever
from mnogobase.retrieval.rerank import ApiReranker
from tests.fakes import FakeEmbedder, FakeSparse
from tests.unit.test_retrieval import StubChunks, _chunk


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
        reranker=StubReranker(RuntimeError("reranker down")),
    )
    assert [i.ref for i in broken.retrieve("q", 2)] == HITS[:2]


def test_api_reranker_sends_raw_texts_once_and_maps_scores_back_to_input_order():
    seen = []

    def handler(request):
        seen.append(request)
        results = [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.2}]
        return httpx.Response(200, json={"results": results})

    settings = RerankSettings(
        base_url="http://rerank:8002/v1",
        model="Qwen/Qwen3-Reranker-4B",
        max_chars=100,
    )
    client = httpx.Client(base_url=settings.url, transport=httpx.MockTransport(handler))
    reranker = ApiReranker(settings, client=client)
    assert reranker.score("q", ["a" * 150, "b", "c"]) == [0.2, 0.9, 0.0]  # c не вернулся
    assert reranker.score("q", []) == []
    assert len(seen) == 1 and str(seen[0].url) == "http://rerank:8002/v1/rerank"
    body = json.loads(seen[0].content)
    assert body == {
        "model": "Qwen/Qwen3-Reranker-4B",
        "query": "q",
        "documents": ["a" * 100, "b", "c"],
        "top_n": 3,
    }


def test_api_reranker_retries_server_errors():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"error": "loading"})
        return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.7}]})

    settings = RerankSettings()
    client = httpx.Client(base_url=settings.url, transport=httpx.MockTransport(handler))
    reranker = ApiReranker(settings, client=client, retry_wait=wait_none())
    assert reranker.score("q", ["a"]) == [0.7]
    assert calls["n"] == 2

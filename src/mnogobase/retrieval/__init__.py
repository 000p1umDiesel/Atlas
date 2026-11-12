"""Поиск контекста для ответа: четыре режима retrieval и их сборка.

Место в потоке данных: вопрос → (необязательный английский перевод из `translate.py`) →
`Retriever.retrieve` выбранного режима → список `ContextItem` → `answer.Answerer` нумерует
элементы, обрезает по бюджету токенов и просит LLM ответить со ссылками `[n]`.

Режимы (`Mode`):

- `rag` (`RagRetriever`) — классический RAG: гибридный поиск (dense + BM25, слияние RRF) по
  исходным чанкам документов в Qdrant, опционально с переранжированием cross-encoder'ом и
  расширением попаданий соседними чанками той же секции.
- `wiki` (`WikiRetriever`) — гибридный поиск по секциям сгенерированных wiki-страниц; за
  каждой секцией идут исходные чанки, которые она цитирует, чтобы ответ ссылался на документы,
  а не только на пересказ.
- `graph` (`GraphRetriever`) — Graph-RAG: находит сущности-«семена» (вектор + полнотекстовый
  индекс Neo4j), обходит их окрестность в графе и возвращает описания сущностей, триплеты
  связей и чанки-подтверждения (плюс, при `graph.use_gds`, чанки из сообществ Leiden).
- `all` (`CombinedRetriever`) — объединение трёх режимов: каждый получает свою долю
  `retrieval.budget` от `retrieval.context_tokens`, одинаковые элементы дедуплицируются.

Все retriever'ы синхронны и ничего не пишут; блокировок не берут.
"""

from __future__ import annotations

from enum import StrEnum

from mnogobase.core.config import Settings
from mnogobase.embedding.base import Embedder, SparseEncoder
from mnogobase.retrieval.base import Retriever
from mnogobase.retrieval.combined import CombinedRetriever
from mnogobase.retrieval.graph import GraphRetriever
from mnogobase.retrieval.rag import RagRetriever
from mnogobase.retrieval.rerank import Reranker
from mnogobase.retrieval.wiki import WikiRetriever
from mnogobase.stores.graph_store import GraphStore
from mnogobase.stores.qdrant_store import QdrantStore


class Mode(StrEnum):
    """Режим поиска; значение — то, что принимают `ask -m`, API и ключи `build_retrievers`."""

    RAG = "rag"
    WIKI = "wiki"
    GRAPH = "graph"
    ALL = "all"


def build_retrievers(
    vectors: QdrantStore,
    graph: GraphStore,
    embedder: Embedder,
    sparse: SparseEncoder,
    settings: Settings,
    reranker: Reranker | None = None,
) -> dict[str, Retriever]:
    """Создаёт по одному retriever на каждый `Mode`; ключ — значение режима (`rag`, `wiki`,
    `graph`, `all`).

    `all` переиспользует те же экземпляры `rag` / `wiki` / `graph`, поэтому настройки
    (соседи, reranker, параметры графа) у него совпадают с одиночными режимами. `reranker`
    передаётся только в `rag`; `None` — без переранжирования.
    """
    rag = RagRetriever(
        vectors,
        embedder,
        sparse,
        settings.retrieval.neighbors,
        reranker,
        settings.rerank.candidates,
    )
    wiki = WikiRetriever(vectors, embedder, sparse, graph)
    graph_retriever = GraphRetriever(graph, vectors, embedder, settings.graph)
    combined = CombinedRetriever(
        {"rag": rag, "wiki": wiki, "graph": graph_retriever},
        settings.retrieval.budget,
        settings.retrieval.context_tokens,
    )
    return {
        Mode.RAG.value: rag,
        Mode.WIKI.value: wiki,
        Mode.GRAPH.value: graph_retriever,
        Mode.ALL.value: combined,
    }

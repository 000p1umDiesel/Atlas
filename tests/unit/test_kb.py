"""KnowledgeBase без PostgreSQL: только логика жизненного цикла App."""

import pytest

from mnogobase.ingest.pipeline import EmbedderMismatchError
from mnogobase.service.kb import KnowledgeBase


class _Pipeline:
    def prepare(self, resume=False):
        raise EmbedderMismatchError("index was built with another embedder")


class _App:
    def __init__(self):
        self.pipeline = _Pipeline()
        self.closed = []

    def close(self):
        self.closed.append("sync")

    async def aclose(self):
        self.closed.append("async")


async def test_app_that_fails_prepare_closes_its_own_clients():
    """Индекс требует reindex: App с открытыми HTTP-клиентами не должен утекать на каждом запросе."""
    app = _App()
    kb = KnowledgeBase.__new__(KnowledgeBase)
    kb.settings, kb._app, kb._factory = None, None, lambda settings: app
    with pytest.raises(EmbedderMismatchError):
        await kb._ensure_app()
    assert app.closed == ["async"]  # aclose закрывает и клиенты моделей и Qdrant
    assert kb._app is None


class _Graph:
    """Граф, в котором одна сущность исчезает между поиском и чтением (параллельное удаление)."""

    def fulltext_entities(self, query, limit, *, prefix=False):
        return [("alive", 1.0), ("gone", 0.5)]

    def get_entity(self, entity_id):
        from mnogobase.core.models import EntityRecord

        if entity_id == "gone":
            return None
        return EntityRecord(entity_id=entity_id, name="Alive", type="Concept")

    def neighborhood(self, seeds, depth, limit):
        from mnogobase.core.models import RelationView

        return [
            RelationView(
                src_id="alive",
                src_name="Alive",
                predicate="uses",
                dst_id="gone",
                dst_name="Gone",
                description="",
                weight=1.0,
                evidence=[],
            )
        ]


async def test_graph_endpoints_skip_entities_removed_meanwhile():
    from types import SimpleNamespace

    from mnogobase.core.log import get_logger
    from mnogobase.service.kb import CoreLoop

    kb = KnowledgeBase.__new__(KnowledgeBase)
    kb._core, kb._log = CoreLoop(), get_logger(__name__)
    kb._app = SimpleNamespace(graph=_Graph())
    try:
        assert [e["entity_id"] for e in await kb.graph_search("alive")] == ["alive"]
        neighbors = await kb.graph_neighbors("alive")
        assert [n["entity_id"] for n in neighbors["nodes"]] == ["alive"]
    finally:
        kb._core.close()

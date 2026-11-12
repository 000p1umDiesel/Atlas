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

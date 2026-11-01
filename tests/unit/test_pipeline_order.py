from types import SimpleNamespace

import pytest

from mnogobase.pipeline import Pipeline


def test_embed_failure_does_not_create_graph_document():
    pipeline = object.__new__(Pipeline)
    documents = []
    pipeline.load_chunks = lambda _: ("document", ["chunk"])
    pipeline._graph = SimpleNamespace(upsert_document=documents.append)

    def fail_index(*_):
        raise RuntimeError("indexing failed")

    pipeline.index_chunks = fail_index
    with pytest.raises(RuntimeError, match="indexing failed"):
        pipeline._embed("doc")
    assert documents == []

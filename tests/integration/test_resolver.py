import json

import pytest
from qdrant_client import QdrantClient

from mnogobase.config import ResolveSettings
from mnogobase.extraction.resolver import EntityResolver
from mnogobase.ids import entity_id
from mnogobase.models import ExtractedEntity
from mnogobase.stores.qdrant_store import QdrantStore
from tests.fakes import FakeLLM, scripted_llm_handler

pytestmark = [
    pytest.mark.integration,
    pytest.mark.filterwarnings(
        "ignore:Payload indexes have no effect in the local Qdrant:UserWarning"
    ),
]

VECTORS = {
    "Transformer": [1.0, 0.0, 0.0],
    "Transformer Architecture": [0.97, 0.243, 0.0],  # cosine 0.97 -> auto merge
    "Transformer Model": [0.85, 0.527, 0.0],  # cosine 0.85 -> ask the LLM
    "Cat": [0.0, 0.0, 1.0],
}


class MapEmbedder:
    model_id = "map"
    dim = 3

    def _vec(self, text: str) -> list[float]:
        return VECTORS[text.split(":")[0].strip()]

    def embed_documents(self, items):
        return [self._vec(i.text) for i in items]

    def embed_query(self, query):
        return self._vec(query)


def E(name, etype="Method", description="d", aliases=()):  # noqa: N802
    return ExtractedEntity(name=name, type=etype, description=description, aliases=list(aliases))


@pytest.fixture
def make_resolver(graph):
    def factory(handler=scripted_llm_handler, **settings):
        vectors = QdrantStore(QdrantClient(":memory:"), "t_", 3)
        vectors.ensure_collections()
        llm = FakeLLM(handler)
        resolver = EntityResolver(graph, vectors, MapEmbedder(), llm, ResolveSettings(**settings))
        return resolver, vectors, llm

    return factory


async def test_new_entity_is_persisted(graph, make_resolver):
    resolver, vectors, _ = make_resolver()
    rec = await resolver.resolve(E("Transformer", description="attention model"))
    assert rec.entity_id == entity_id("Method", "Transformer")
    assert graph.get_entity(rec.entity_id).description == "attention model"
    assert vectors.client.count(vectors.entities).count == 1


async def test_exact_id_merges_aliases_and_descriptions(graph, make_resolver):
    resolver, _, _ = make_resolver()
    first = await resolver.resolve(E("Transformer", description="one"))
    again = await resolver.resolve(E("transformer", description="two", aliases=["Трансформер"]))
    assert again.entity_id == first.entity_id
    stored = graph.get_entity(first.entity_id)
    assert stored.aliases == ["Трансформер"]
    assert stored.descriptions == ["one", "two"]


async def test_vector_auto_merge(graph, make_resolver):
    resolver, _, llm = make_resolver()
    base = await resolver.resolve(E("Transformer"))
    merged = await resolver.resolve(E("Transformer Architecture", etype="Concept"))
    assert merged.entity_id == base.entity_id
    assert "Transformer Architecture" in graph.get_entity(base.entity_id).aliases
    assert llm.calls_for("resolve") == []


@pytest.mark.parametrize("same", [True, False])
async def test_llm_check_band(graph, make_resolver, same):
    def handler(task, prompt):
        return json.dumps({"same": same, "reason": "r"})

    resolver, _, llm = make_resolver(handler)
    base = await resolver.resolve(E("Transformer"))
    other = await resolver.resolve(E("Transformer Model"))
    assert len(llm.calls_for("resolve")) == 1
    assert (other.entity_id == base.entity_id) is same


async def test_far_entity_is_new(graph, make_resolver):
    resolver, _, _ = make_resolver()
    a = await resolver.resolve(E("Transformer"))
    b = await resolver.resolve(E("Cat", etype="Concept"))
    assert a.entity_id != b.entity_id


async def test_descriptions_are_summarized(graph, make_resolver):
    resolver, _, llm = make_resolver(max_descriptions=2)
    for text in ("one", "two", "three"):
        rec = await resolver.resolve(E("Transformer", description=text))
    assert rec.description == "Merged description."
    assert graph.get_entity(rec.entity_id).descriptions == ["Merged description."]
    assert len(llm.calls_for("resolve")) == 1


class FlakyEmbedder(MapEmbedder):
    """Fails `embed_documents` once `budget` successful calls are used up."""

    def __init__(self, budget: int):
        self.budget = budget

    def embed_documents(self, items):
        if self.budget <= 0:
            raise RuntimeError("embedder down")
        self.budget -= 1
        return super().embed_documents(items)


async def test_embedder_failure_leaves_no_graph_entity_without_vector(graph, make_resolver):
    resolver, vectors, _ = make_resolver()
    resolver._embedder = FlakyEmbedder(budget=1)  # the candidate search works, the write fails
    with pytest.raises(RuntimeError, match="embedder down"):
        await resolver.resolve(E("Transformer", description="attention model"))
    assert graph.get_entity(entity_id("Method", "Transformer")) is None
    assert vectors.client.count(vectors.entities).count == 0

    resolver._embedder = MapEmbedder()  # a retry creates both
    rec = await resolver.resolve(E("Transformer", description="attention model"))
    assert graph.get_entity(rec.entity_id) is not None
    assert vectors.client.count(vectors.entities).count == 1


async def test_embedder_failure_on_merge_keeps_graph_unchanged(graph, make_resolver):
    resolver, vectors, _ = make_resolver()
    first = await resolver.resolve(E("Transformer", description="one"))
    resolver._embedder = FlakyEmbedder(budget=0)
    with pytest.raises(RuntimeError, match="embedder down"):
        await resolver.resolve(E("Transformer", description="two"))
    # the graph is not ahead of Qdrant, so the retry still sees a change and writes both
    assert graph.get_entity(first.entity_id).descriptions == ["one"]
    resolver._embedder = MapEmbedder()
    await resolver.resolve(E("Transformer", description="two"))
    assert graph.get_entity(first.entity_id).descriptions == ["one", "two"]
    assert vectors.client.count(vectors.entities).count == 1

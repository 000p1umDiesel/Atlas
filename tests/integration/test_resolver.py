import json

import pytest
from qdrant_client import QdrantClient

from mnogobase.core.config import ResolveSettings
from mnogobase.core.ids import entity_id
from mnogobase.core.models import ExtractedEntity
from mnogobase.extraction.resolver import EntityResolver
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
    "Transformer Architecture": [0.97, 0.243, 0.0],  # косинус 0.97 -> автослияние
    "Transformer Model": [0.85, 0.527, 0.0],  # косинус 0.85 -> спросить LLM
    "Cat": [0.0, 0.0, 1.0],
    "MagAO-X": [0.0, 1.0, 0.0],
    "MagAOX": [0.6, 0.0, 0.8],  # косинус 0 с "MagAO-X": найти можно только по имени
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
    resolver, _, llm = make_resolver(auto_merge=0.92)
    base = await resolver.resolve(E("Transformer"))
    merged = await resolver.resolve(E("Transformer Architecture", etype="Concept"))
    assert merged.entity_id == base.entity_id
    assert "Transformer Architecture" in graph.get_entity(base.entity_id).aliases
    assert llm.calls_for("resolve") == []


async def test_high_score_goes_to_llm_by_default(graph, make_resolver):
    resolver, _, llm = make_resolver()
    base = await resolver.resolve(E("Transformer"))
    other = await resolver.resolve(E("Transformer Architecture", etype="Concept"))
    assert len(llm.calls_for("resolve")) == 1
    assert other.entity_id != base.entity_id  # scripted_llm_handler отвечает "не то же"


@pytest.mark.parametrize("same", [True, False])
async def test_name_key_candidate_is_checked_regardless_of_score(graph, make_resolver, same):
    def handler(task, prompt):
        return json.dumps({"same": same, "reason": "r"})

    resolver, _, llm = make_resolver(handler)
    base = await resolver.resolve(E("MagAO-X", etype="Equipment"))
    other = await resolver.resolve(E("MagAOX", etype="Technology"))
    assert len(llm.calls_for("resolve")) == 1
    assert (other.entity_id == base.entity_id) is same


async def test_alias_key_candidate(graph, make_resolver):
    def handler(task, prompt):
        return json.dumps({"same": True, "reason": "r"})

    resolver, _, _ = make_resolver(handler)
    base = await resolver.resolve(E("MagAO-X", aliases=["Cat"]))
    other = await resolver.resolve(E("Cat", etype="Concept"))
    assert other.entity_id == base.entity_id


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
    """Падает в `embed_documents`, когда исчерпаны `budget` успешных вызовов."""

    def __init__(self, budget: int):
        self.budget = budget

    def embed_documents(self, items):
        if self.budget <= 0:
            raise RuntimeError("embedder down")
        self.budget -= 1
        return super().embed_documents(items)


async def test_embedder_failure_leaves_no_graph_entity_without_vector(graph, make_resolver):
    resolver, vectors, _ = make_resolver()
    resolver._embedder = FlakyEmbedder(budget=1)  # поиск кандидатов работает, запись падает
    with pytest.raises(RuntimeError, match="embedder down"):
        await resolver.resolve(E("Transformer", description="attention model"))
    assert graph.get_entity(entity_id("Method", "Transformer")) is None
    assert vectors.client.count(vectors.entities).count == 0

    resolver._embedder = MapEmbedder()  # retry создаёт обе
    rec = await resolver.resolve(E("Transformer", description="attention model"))
    assert graph.get_entity(rec.entity_id) is not None
    assert vectors.client.count(vectors.entities).count == 1


async def test_embedder_failure_on_merge_keeps_graph_unchanged(graph, make_resolver):
    resolver, vectors, _ = make_resolver()
    first = await resolver.resolve(E("Transformer", description="one"))
    resolver._embedder = FlakyEmbedder(budget=0)
    with pytest.raises(RuntimeError, match="embedder down"):
        await resolver.resolve(E("Transformer", description="two"))
    # граф не опережает Qdrant, поэтому retry всё ещё видит изменение и записывает обе
    assert graph.get_entity(first.entity_id).descriptions == ["one"]
    resolver._embedder = MapEmbedder()
    await resolver.resolve(E("Transformer", description="two"))
    assert graph.get_entity(first.entity_id).descriptions == ["one", "two"]
    assert vectors.client.count(vectors.entities).count == 1


def answer(same):
    def handler(task, prompt):
        return json.dumps({"same": same, "reason": "r"})

    return handler


@pytest.mark.parametrize("same", [True, False])
async def test_prefetch_answers_graph_candidates_ahead_of_resolve(graph, make_resolver, same):
    resolver, _, llm = make_resolver(answer(same))
    base = await resolver.resolve(E("Transformer"))
    resolver.forget()  # новый документ
    doc = [E("Transformer Model"), E("Transformer"), E("Transformer Model", description="d2")]
    assert await resolver.prefetch(doc) == 1  # "Transformer" уже в графе, повтор id не нужен
    assert len(llm.calls_for("resolve")) == 1
    records = [await resolver.resolve(e) for e in doc]
    assert len(llm.calls_for("resolve")) == 1  # resolve взял ответ из заготовки
    assert (resolver.verdict_hits, resolver.verdict_misses) == (1, 0)
    assert (records[0].entity_id == base.entity_id) is same


@pytest.mark.parametrize("same", [True, False])
async def test_prefetch_checks_new_entities_of_the_same_document(graph, make_resolver, same):
    resolver, _, llm = make_resolver(answer(same))
    doc = [E("Transformer"), E("Transformer Model"), E("Cat", etype="Concept")]
    # графа нет: единственная пара — "Transformer Model" против более раннего "Transformer"
    assert await resolver.prefetch(doc) == 1
    first, second, cat = [await resolver.resolve(e) for e in doc]
    assert len(llm.calls_for("resolve")) == 1
    assert (second.entity_id == first.entity_id) is same
    assert cat.entity_id not in (first.entity_id, second.entity_id)


async def test_prefetch_pairs_by_name_key_regardless_of_score(graph, make_resolver):
    resolver, _, llm = make_resolver(answer(True))
    doc = [E("MagAO-X", etype="Equipment"), E("MagAOX", etype="Technology")]
    assert await resolver.prefetch(doc) == 1
    a, b = [await resolver.resolve(e) for e in doc]
    assert a.entity_id == b.entity_id and len(llm.calls_for("resolve")) == 1


async def test_resolve_asks_llm_when_prefetch_missed(graph, make_resolver):
    resolver, _, llm = make_resolver(answer(False))
    await resolver.resolve(E("Transformer"))
    await resolver.resolve(E("Transformer Model"))  # кандидат появился без заготовки
    assert (resolver.verdict_hits, resolver.verdict_misses) == (0, 1)
    resolver.forget()
    assert (resolver.verdict_hits, resolver.verdict_misses) == (0, 0)


async def test_prefetch_failure_raises_the_original_error(graph, make_resolver):
    def handler(task, prompt):
        raise ConnectionError("llm down")

    resolver, _, _ = make_resolver(handler)
    await resolver.prefetch([E("Cat", etype="Concept")])  # нечего спрашивать — LLM не нужен
    with pytest.raises(ConnectionError, match="llm down"):
        await resolver.prefetch([E("Transformer"), E("Transformer Model")])

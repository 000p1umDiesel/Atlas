"""KnowledgeBase поверх представлений графа: статистика, типы, каталог и страница сущности."""

import pytest
from qdrant_client import QdrantClient

from mnogobase.app import build_app
from mnogobase.core.config import load_settings
from mnogobase.core.database import database_target
from mnogobase.service.errors import ServiceError
from mnogobase.service.kb import KnowledgeBase
from mnogobase.stores.graph_store import DRIVER_OPTIONS, GraphStore
from mnogobase.stores.qdrant_store import QdrantStore
from mnogobase.stores.registry import Registry
from tests.fakes import FakeEmbedder, FakeLLM, FakeSparse, scripted_llm_handler
from tests.integration.graph_seed import VIEW_DOC_A, populate_views, publish_views_analysis

pytestmark = [
    pytest.mark.integration,
    pytest.mark.filterwarnings(
        "ignore:Payload indexes have no effect in the local Qdrant:UserWarning"
    ),
]


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "wiki: {dir: wiki}\nservice: {uploads_dir: documents/uploads}\n", encoding="utf-8"
    )
    return load_settings(tmp_path / "config.yaml")


@pytest.fixture
def factory(neo4j_container):
    shared = QdrantClient(":memory:")

    def build(current):
        return build_app(
            current,
            embedder=FakeEmbedder(),
            sparse=FakeSparse(),
            llm=FakeLLM(scripted_llm_handler),
            vectors=QdrantStore(shared, "t_", 64),
            graph=GraphStore(neo4j_container.get_driver(**DRIVER_OPTIONS)),
        )

    return build


@pytest.fixture
async def kbv(graph, settings, factory, tmp_path):
    """База на графе `populate_views` с актуальным анализом; документ A загружен через API."""
    populate_views(graph)
    publish_views_analysis(graph)
    kb = KnowledgeBase(settings, app_factory=factory)
    kb.uploads.add(VIEW_DOC_A, "Отчёт.pdf", tmp_path / f"{VIEW_DOC_A}.pdf")
    try:
        yield kb
    finally:
        await kb.close()


@pytest.fixture
async def kb_empty(graph, settings, factory):
    """Свежая установка: пустой граф, registry и загрузки, анализа не было."""
    kb = KnowledgeBase(settings, app_factory=factory)
    try:
        yield kb
    finally:
        await kb.close()


async def test_stats_and_types(kbv):
    s = await kbv.stats()
    assert (s["chunks"], s["entities"], s["relations"], s["wiki_pages"]) == (5, 6, 5, 1)
    assert s["analysis"]["ready"] is True and s["analysis"]["completed_at"]
    # загрузка, которую worker ещё не взял, — документ в очереди, первым в recent
    assert s["documents"] == 1 and s["documents_by_status"] == {"queued": 1}
    assert s["recent"] == [
        {"id": VIEW_DOC_A, "name": "Отчёт.pdf", "status": "queued", "updated_at": None}
    ]
    types = await kbv.entity_types()
    assert len(types) == 18
    assert (types[0]["type"], types[0]["label"], types[0]["count"]) == ("Person", "Человек", 2)


async def test_stats_counts_registry_rows(kbv, settings):
    registry = Registry(database_target(settings))
    try:
        registry.upsert_file("/data/beta.md", "b" * 16, 1, 1.0, "done")
        for n in range(10):
            registry.upsert_file(f"/data/f{n}.md", f"{n:x}" * 16, 1, 1.0, "failed")
    finally:
        registry.close()
    s = await kbv.stats()
    assert s["documents"] == 12
    assert s["documents_by_status"] == {"queued": 1, "done": 1, "failed": 10}
    recent = s["recent"]
    assert len(recent) == 8
    assert recent[0]["id"] == VIEW_DOC_A and recent[0]["updated_at"] is None
    assert recent[1]["name"] == "f9.md" and recent[1]["updated_at"]  # новые первыми
    assert all("/" not in r["name"] and "path" not in r for r in recent)


async def test_stats_and_types_on_empty_graph(kb_empty):
    s = await kb_empty.stats()
    assert s["entities"] == 0 and s["documents"] == 0 and s["recent"] == []
    assert (s["chunks"], s["relations"], s["wiki_pages"]) == (0, 0, 0)
    assert s["analysis"] == {"ready": False, "completed_at": None}
    types = await kb_empty.entity_types()
    assert len(types) == 18 and all(t["count"] == 0 for t in types)


async def test_entity_catalog_and_page(kbv):
    page = await kbv.entities(q="love")
    assert page["total"] == 1 and page["items"][0]["entity_id"] == "ada"
    assert (await kbv.entities(sort="name"))["items"][0]["name"] == "Ada Lovelace"
    card = await kbv.entity("ada")
    assert card["degree"] == 3 and card["documents_count"] == 2 and card["has_page"] is True
    assert card["pagerank"] == 0.30 and card["slug"] == "ada-lovelace"
    mentions = await kbv.entity_mentions("ada")
    assert mentions["total"] == 3 and {m["name"] for m in mentions["items"]} == {
        "Отчёт.pdf",
        "beta.md",
    }
    assert all("path" not in m for m in mentions["items"])
    by_name = {m["name"]: m["url"] for m in mentions["items"]}
    assert by_name["Отчёт.pdf"] == f"/api/documents/{VIEW_DOC_A}/content"
    assert by_name["beta.md"] is None
    docs = await kbv.entity_documents("ada")
    assert [(d["name"], d["mentions"]) for d in docs] == [("Отчёт.pdf", 2), ("beta.md", 1)]
    assert docs[0]["url"] and docs[1]["url"] is None
    assert all(set(d) == {"doc_id", "name", "mentions", "url"} for d in docs)
    relations = await kbv.entity_relations("ada", direction="out")
    assert relations["total"] == len(relations["items"]) > 0
    assert (await kbv.entity_mentions("ada", limit=1, offset=2))["total"] == 3


def ids(page: dict) -> list[str]:
    return [e["entity_id"] for e in page["items"]]


async def test_catalog_defaults_and_blank_query(kbv):
    everything = await kbv.entities()
    assert everything["total"] == 6
    assert ids(everything) == ["ada", "engine", "babbage", "loom", "lonely", "notes"]
    # пустая строка поиска — весь каталог, а не пустой результат
    for blank in ("", "   "):
        assert await kbv.entities(q=blank) == everything
    assert ids(await kbv.entities(sort="mentions", order="asc"))[:3] == ["loom", "lonely", "notes"]
    assert ids(await kbv.entities(sort="name", order="desc"))[-1] == "ada"
    assert ids(await kbv.entities(sort="degree"))[:3] == ["ada", "engine", "babbage"]
    assert ids(await kbv.entities(limit=2, offset=2)) == ["babbage", "loom"]
    typed = await kbv.entities(types=["Person"])
    assert typed["total"] == 2 and {e["type"] for e in typed["items"]} == {"Person"}


@pytest.mark.parametrize(
    "call", ["entity", "entity_relations", "entity_mentions", "entity_documents"]
)
async def test_unknown_entity_is_404(kbv, call):
    with pytest.raises(ServiceError) as err:
        await getattr(kbv, call)("missing")
    assert err.value.status == 404 and err.value.detail == "Сущность не найдена"

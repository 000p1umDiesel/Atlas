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


async def test_document_card_entities_from_mentions_and_registry(kbv, settings):
    """Сущности карточки — объединение MENTIONS чанков и registry `doc_entities`."""
    failed = "c" * 16
    registry = Registry(database_target(settings))
    try:
        # документ A: упоминания в графе есть, журнал `doc_entities` пуст (старая база)
        registry.upsert_file("/data/alpha.md", VIEW_DOC_A, 1, 1.0, "done")
        # документ после сбоя стадии graph: сущность записана, упоминаний ещё нет
        registry.upsert_file("/data/broken.md", failed, 1, 1.0, "failed")
        registry.add_doc_entity(failed, "loom")
    finally:
        registry.close()

    card = await kbv.document(VIEW_DOC_A)
    assert [e["entity_id"] for e in card["entities"]] == ["ada", "engine", "babbage", "notes"]
    ada = next(e for e in card["entities"] if e["entity_id"] == "ada")
    assert ada == {
        "entity_id": "ada",
        "name": "Ada Lovelace",
        "type": "Person",
        "has_page": True,
        "slug": "ada-lovelace",
    }

    broken = await kbv.document(failed)
    assert broken["status"] == "failed" and broken["chunks"] == 0
    assert [e["entity_id"] for e in broken["entities"]] == ["loom"]


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


@pytest.fixture
def owner(kbv):
    return kbv.store.create_user("owner", "long-enough-password")


async def test_overview_path_communities(kbv):
    overview = await kbv.graph_overview(limit=3)
    assert overview["ranked_by"] == "pagerank"
    assert [n["entity_id"] for n in overview["nodes"]] == ["ada", "babbage", "engine"]
    # пробельное сообщество — без фильтра, как пустой `q` каталога
    assert await kbv.graph_overview(community="  ") == await kbv.graph_overview()
    only_x = await kbv.graph_overview(community="community:x", types=["Person"])
    assert {n["entity_id"] for n in only_x["nodes"]} == {"ada", "babbage"}
    path = await kbv.graph_path("notes", "loom")
    assert path["timed_out"] is False
    assert [n["entity_id"] for n in path["paths"][0]["nodes"]][-1] == "loom"
    assert (await kbv.graph_path("notes", "lonely")) == {"paths": [], "timed_out": False}
    for args, code, detail in (
        (("ada", "ada"), 422, "Выберите две разные сущности"),
        (("ada", "missing"), 404, "Сущность не найдена"),
        (("missing", "ada"), 404, "Сущность не найдена"),
    ):
        with pytest.raises(ServiceError) as err:
            await kbv.graph_path(*args)
        assert (err.value.status, err.value.detail) == (code, detail)
    communities = await kbv.graph_communities()
    assert communities["ready"] is True and communities["total"] == 3
    assert communities["items"][0]["community"] == "community:x"
    members = await kbv.graph_community("community:y")
    assert {n["entity_id"] for n in members["nodes"]} == {"engine", "loom"}
    with pytest.raises(ServiceError) as err:
        await kbv.graph_community("community:nope")
    assert (err.value.status, err.value.detail) == (404, "Сообщество не найдено")


async def test_neighbors_carry_node_view_fields(kbv):
    nb = await kbv.graph_neighbors("ada")
    assert set(nb) == {"nodes", "relations"}
    ada = next(n for n in nb["nodes"] if n["entity_id"] == "ada")
    assert ada["degree"] == 3 and ada["pagerank"] == 0.30 and "aliases" in ada
    assert "description" in ada and ada["community"] == "community:x"
    assert ada["has_page"] is True and ada["slug"] == "ada-lovelace"
    assert {n["entity_id"] for n in nb["nodes"]} == {"ada", "babbage", "engine", "notes"}


async def test_quick_search(kbv, owner):
    kbv.store.create_conversation(owner["id"], "Про Аду")
    kbv.store.create_conversation(owner["id"], "Другое")
    found = await kbv.quick_search(owner["id"], "ada")
    assert set(found) == {"entities", "wiki", "documents", "conversations"}
    assert [e["entity_id"] for e in found["entities"]] == ["ada"]
    assert found["entities"][0]["pagerank"] == 0.30
    assert found["wiki"] == [{"page_id": "ada", "slug": "ada-lovelace", "title": "Ada Lovelace"}]
    assert found["documents"] == []
    russian = await kbv.quick_search(owner["id"], "отч")
    assert russian["documents"] == [{"id": VIEW_DOC_A, "name": "Отчёт.pdf", "status": "queued"}]
    assert [c["title"] for c in (await kbv.quick_search(owner["id"], "аду"))["conversations"]] == [
        "Про Аду"
    ]
    assert set((await kbv.quick_search(owner["id"], "ду"))["conversations"][0]) == {"id", "title"}
    for q in ("%", '"', "\\", "(*", "_", "   "):
        result = await kbv.quick_search(owner["id"], q)  # без исключений
        assert result["conversations"] == [] and result["documents"] == []


async def test_quick_search_caps_each_group(kbv, owner):
    for n in range(7):
        kbv.store.create_conversation(owner["id"], f"engine {n}")
        kbv.uploads.add(f"{n:x}" * 16, f"engine-{n}.md", kbv.uploads.root / f"{n}.md")
    found = await kbv.quick_search(owner["id"], "engine")
    assert len(found["conversations"]) == 5 and len(found["documents"]) == 5
    assert found["conversations"][0]["title"] == "engine 6"  # новые первыми


async def test_overview_and_communities_on_empty_graph(kb_empty):
    overview = await kb_empty.graph_overview()
    assert overview["nodes"] == [] and overview["ranked_by"] == "mentions"
    assert (await kb_empty.graph_communities()) == {"ready": False, "total": 0, "items": []}
    with pytest.raises(ServiceError) as err:
        await kb_empty.graph_community("community:x")
    assert err.value.status == 404


async def test_quick_search_blank_does_not_touch_app(kb_empty, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("ядро не должно создаваться")

    monkeypatch.setattr(kb_empty, "_ensure_app", boom, raising=False)
    for q in ("", "   "):
        assert await kb_empty.quick_search("u", q) == {
            "entities": [],
            "wiki": [],
            "documents": [],
            "conversations": [],
        }


async def test_blank_community_and_predicate_mean_no_filter(kbv):
    everything = await kbv.entities()
    for blank in ("", "  "):
        assert await kbv.entities(community=blank) == everything
    unfiltered = await kbv.entity_relations("ada")
    for blank in ("", "  "):
        assert await kbv.entity_relations("ada", predicate=blank) == unfiltered

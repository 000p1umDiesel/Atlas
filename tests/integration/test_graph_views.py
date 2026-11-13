"""Представления узлов графа для веба: карточки узлов, счётчики типов и каталог сущностей."""

import pytest

from mnogobase.core.models import EntityRecord
from tests.integration.graph_seed import (
    VIEW_DOC_A,
    VIEW_DOC_B,
    populate_views,
    publish_views_analysis,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def views(graph):
    """Граф `populate_views` с актуальной аналитикой `publish_views_analysis`."""
    populate_views(graph)
    publish_views_analysis(graph)
    return graph


def ids(rows):
    return [r["entity_id"] for r in rows]


def test_node_views_fields_order_and_missing(views):
    rows = views.node_views(["engine", "missing", "ada"])
    assert ids(rows) == ["engine", "ada"]
    ada = rows[1]
    expected = {
        "name": "Ada Lovelace",
        "type": "Person",
        "mention_count": 3,
        "pagerank": 0.30,
        "community": "community:x",
        "degree": 3,
        "aliases": ["Lovelace"],
        "has_page": True,
        "slug": "ada-lovelace",
    }
    assert {k: ada[k] for k in expected} == expected
    assert views.node_views(["babbage"])[0]["has_page"] is False  # страница stale
    assert views.node_views([]) == []


def test_stale_analysis_hides_scores(views):
    views.upsert_entity(EntityRecord(entity_id="new", name="New", type="Concept"))
    ada = views.node_views(["ada"])[0]
    assert ada["pagerank"] is None and ada["community"] is None


def test_entity_type_counts(views):
    assert views.entity_type_counts() == {"Person": 2, "Equipment": 2, "Work": 1, "Concept": 1}


def test_empty_graph(graph):
    assert graph.entity_type_counts() == {}
    assert graph.list_entities() == (0, [])
    assert graph.list_entities(q="ada") == (0, [])
    assert graph.node_views(["ada"]) == []


def test_list_entities_sorting_and_paging(views):
    total, rows = views.list_entities()
    assert total == 6 and ids(rows) == ["ada", "engine", "babbage", "loom", "lonely", "notes"]
    assert ids(views.list_entities(sort="degree")[1])[:3] == ["ada", "engine", "babbage"]
    assert ids(views.list_entities(sort="pagerank")[1])[:3] == ["ada", "babbage", "engine"]
    assert ids(views.list_entities(sort="name", descending=False)[1])[0] == "ada"
    assert ids(views.list_entities(sort="mentions", descending=False)[1])[:3] == [
        "loom",
        "lonely",
        "notes",
    ]
    total, page = views.list_entities(limit=2, offset=2)
    assert total == 6 and ids(page) == ["babbage", "loom"]
    assert views.list_entities(offset=10) == (6, [])


def test_list_entities_rows_are_node_views(views):
    row = views.list_entities(q="lovelace")[1][0]
    assert row == views.node_views(["ada"])[0]


def test_list_entities_filters(views):
    assert views.list_entities(types=["Person"])[0] == 2
    assert ids(views.list_entities(min_mentions=2)[1]) == ["ada", "engine", "babbage"]
    assert set(ids(views.list_entities(community="community:y")[1])) == {"engine", "loom"}
    assert ids(views.list_entities(q="love")[1]) == ["ada"]  # префикс алиаса
    assert set(ids(views.list_entities(q="engine")[1])) == {"engine", "notes"}
    assert ids(views.list_entities(q="engine", types=["Work"])[1]) == ["notes"]


def test_list_entities_relevance(views):
    # без q сортировка по релевантности = по упоминаниям
    assert ids(views.list_entities(sort="relevance")[1]) == ids(views.list_entities()[1])
    total, rows = views.list_entities(q="engine", sort="relevance")
    assert total == 2 and set(ids(rows)) == {"engine", "notes"}


@pytest.mark.parametrize("q", ["%", "_", '"', "*", "\\", "(", "AND OR"])
def test_list_entities_special_characters(views, q):
    total, rows = views.list_entities(q=q)
    assert total == len(rows)  # не падает; без словесных токенов — (0, [])


def test_pagerank_sort_and_community_filter_without_current_run(views):
    views.upsert_entity(EntityRecord(entity_id="new", name="New", type="Concept"))
    assert views.list_entities(community="community:y") == (0, [])
    rows = views.list_entities(sort="pagerank")[1]
    assert (
        all(r["pagerank"] is None for r in rows) and ids(rows)[0] == "ada"
    )  # дальше по упоминаниям


def test_entity_relations_directions_and_facets(views):
    rel = views.entity_relations("ada")
    assert rel["total"] == 3
    assert [r["predicate"] for r in rel["items"]] == ["collaborated_with", "wrote", "described"]
    first = rel["items"][0]
    assert (first["src_type"], first["dst_type"], first["weight"]) == ("Person", "Person", 6.0)
    assert rel["predicates"] == [
        {"predicate": p, "count": 1} for p in ("collaborated_with", "described", "wrote")
    ]
    assert views.entity_relations("ada", direction="in")["total"] == 0
    babbage_in = views.entity_relations("babbage", direction="in")["items"]
    assert [r["predicate"] for r in babbage_in] == ["collaborated_with"]
    only = views.entity_relations("ada", predicate="wrote")
    assert only["total"] == 1 and len(only["predicates"]) == 3
    assert views.entity_relations("ada", limit=1, offset=1)["items"][0]["predicate"] == "wrote"
    assert views.entity_relations("missing") == {"total": 0, "predicates": [], "items": []}


def test_entity_mentions_and_documents(views):
    total, chunks = views.entity_mentions("ada")
    assert total == 3
    assert [c.chunk_id for c in chunks] == [
        f"{VIEW_DOC_A}:00000",
        f"{VIEW_DOC_A}:00001",
        f"{VIEW_DOC_B}:00000",
    ]
    page = views.entity_mentions("ada", limit=1, offset=2)[1]
    assert [c.chunk_id for c in page] == [f"{VIEW_DOC_B}:00000"]
    assert views.entity_mentions("missing") == (0, [])
    docs = views.entity_documents("ada")
    assert [(d["doc_id"], d["path"], d["mentions"]) for d in docs] == [
        (VIEW_DOC_A, "/alpha.md", 2),
        (VIEW_DOC_B, "/beta.md", 1),
    ]
    assert views.entity_document_count("ada") == 2
    assert views.entity_document_count("missing") == 0


def test_overview_ranks_by_pagerank_and_truncates(views):
    ov = views.overview(limit=3)
    assert ov["ranked_by"] == "pagerank" and ov["truncated"] and ov["total_nodes"] == 6
    assert ids(ov["nodes"]) == ["ada", "babbage", "engine"]
    assert [r["predicate"] for r in ov["relations"]] == [
        "designed",
        "collaborated_with",
        "described",
    ]
    assert [r["predicate"] for r in views.overview(min_weight=5)["relations"]] == [
        "designed",
        "collaborated_with",
        "wrote",
    ]
    eq = views.overview(types=["Equipment"])
    assert set(ids(eq["nodes"])) == {"engine", "loom"} and not eq["truncated"]
    assert views.overview(max_relations=2)["truncated"]
    assert set(ids(views.overview(community="community:y")["nodes"])) == {"engine", "loom"}
    assert views.overview(min_mentions=2)["total_nodes"] == 3


def test_overview_falls_back_to_mentions_when_analysis_is_stale(views):
    views.upsert_entity(EntityRecord(entity_id="new", name="New", type="Concept"))
    ov = views.overview(limit=3)
    assert ov["ranked_by"] == "mentions" and ids(ov["nodes"]) == ["ada", "engine", "babbage"]


def test_overview_on_empty_graph(graph):
    assert graph.overview() == {
        "nodes": [],
        "relations": [],
        "ranked_by": "mentions",
        "truncated": False,
        "total_nodes": 0,
    }


def test_shortest_paths(views):
    paths, timed_out = views.shortest_paths("notes", "loom")
    assert not timed_out and len(paths) == 1
    assert ids(paths[0]["nodes"]) == ["notes", "ada", "engine", "loom"]
    assert [r["predicate"] for r in paths[0]["relations"]] == ["wrote", "described", "inspired_by"]
    wrote = paths[0]["relations"][0]  # связь в хранимом направлении, не по ходу пути
    assert (wrote["src_id"], wrote["dst_id"], wrote["dst_type"]) == ("ada", "notes", "Work")
    assert views.shortest_paths("notes", "loom", max_hops=2) == ([], False)
    assert views.shortest_paths("ada", "lonely") == ([], False)
    assert views.shortest_paths("ada", "missing") == ([], False)
    assert views.shortest_paths("ada", "ada") == ([], False)


def test_communities_and_subgraph(views):
    c = views.communities()
    assert c["ready"] and c["total"] == 3
    assert [(i["community"], i["size"]) for i in c["items"]] == [
        ("community:x", 3),
        ("community:y", 2),
        ("community:z", 1),
    ]
    assert ids(c["items"][0]["top"]) == ["ada", "babbage", "notes"]
    assert c["items"][0]["types"] == {"Person": 2, "Work": 1}
    sub = views.community_subgraph("community:y")
    assert set(ids(sub["nodes"])) == {"engine", "loom"} and [
        r["predicate"] for r in sub["relations"]
    ] == ["inspired_by"]
    assert views.community_subgraph("community:x", limit=2)["truncated"] is True
    assert views.community_subgraph("community:nope") is None
    views.upsert_entity(EntityRecord(entity_id="new", name="New", type="Concept"))
    assert views.communities() == {"ready": False, "total": 0, "items": []}
    assert views.community_subgraph("community:y") is None

"""Представления узлов графа для веба: карточки узлов, счётчики типов и каталог сущностей."""

import pytest

from mnogobase.core.models import EntityRecord
from tests.integration.graph_seed import populate_views, publish_views_analysis

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

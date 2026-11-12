import pytest
from pydantic import ValidationError

from mnogobase.core.config import GraphSettings


def test_graph_extensions_are_opt_in_for_library_users():
    settings = GraphSettings()
    assert settings.traversal == "cypher"
    assert settings.use_gds is False
    assert settings.analysis.memory_mb == 1536


@pytest.mark.parametrize(
    "overrides",
    [
        {"traversal": "arbitrary-procedure"},
        {"analysis": {"max_nodes": 0}},
        {"analysis": {"max_relationships": -1}},
        {"analysis": {"memory_mb": 0}},
        {"analysis": {"timeout_s": 0}},
        {"analysis": {"apoc_max_nodes": 0}},
        {"community": {"max_communities": 0}},
        {"community": {"max_entities": 0}},
        {"community": {"max_chunks": 0}},
        {"community": {"timeout_s": 0}},
        {"community": {"timeout_s": float("inf")}},
    ],
)
def test_graph_extensions_reject_unsafe_budgets(overrides):
    with pytest.raises(ValidationError):
        GraphSettings(**overrides)


def test_algorithm_memory_is_added_to_projection_memory():
    from mnogobase.ops.graph_analysis import AnalysisError, check_memory

    check_memory(700_000, 300_000, budget_mb=1)
    with pytest.raises(AnalysisError, match="памяти"):
        check_memory(700_000, 400_000, budget_mb=1)


def test_community_keys_follow_membership_not_leiden_integer_ids():
    from mnogobase.ops.graph_analysis import community_keys

    first = community_keys(
        [{"entity_id": "b", "community_id": 8}, {"entity_id": "a", "community_id": 8}]
    )
    second = community_keys(
        [{"entity_id": "a", "community_id": 97}, {"entity_id": "b", "community_id": 97}]
    )
    assert first == second
    assert first["a"] == first["b"]


def test_community_query_timeout_keeps_normal_retrieval_available():
    from neo4j.exceptions import Neo4jError

    from mnogobase.stores.graph_store import GraphStore

    store = GraphStore(None)

    def fail(*args, **kwargs):
        raise Neo4jError._hydrate_neo4j(
            code="Neo.ClientError.Transaction.TransactionTimedOut", message="expired"
        )

    store._run = fail
    assert store.community_chunks(["seed"], 2) == []

    def unauthorized(*args, **kwargs):
        raise Neo4jError._hydrate_neo4j(
            code="Neo.ClientError.Security.Unauthorized", message="unauthorized"
        )

    store._run = unauthorized
    with pytest.raises(Neo4jError):
        store.community_chunks(["seed"], 2)


def test_apoc_traversal_timeout_falls_back_to_one_hop_cypher():
    """Таймаут обхода APOC не роняет ответ в режимах graph/all: берётся сосед в один шаг."""
    from neo4j.exceptions import Neo4jError

    from mnogobase.stores.graph_store import GraphStore

    store = GraphStore(None)

    def run(query, **params):
        if "count(e)" in query:
            return [{"n": 10}]
        raise Neo4jError._hydrate_neo4j(
            code="Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration",
            message="timed out",
        )

    fallback = []
    store._run = run
    store.neighborhood = lambda seeds, hops, limit: fallback.append((seeds, hops, limit)) or []
    assert store.neighborhood_apoc(["seed"], 3, 40) == []
    assert fallback == [(["seed"], 1, 40)]


def test_fulltext_query_is_deduplicated_and_bounded():
    """Длинный вопрос не превышает лимит клауз Lucene: слова без повторов и не больше лимита."""
    from mnogobase.stores.graph_store import FULLTEXT_MAX_TOKENS, GraphStore

    store = GraphStore(None)
    seen = {}
    store._run = lambda query, **params: seen.update(params) or []
    words = [f"word{i}" for i in range(2000)]
    store.fulltext_entities(" ".join(["Attention", "attention", *words]), 5)
    tokens = seen["q"].split()
    assert tokens[:2] == ["attention", "word0"]
    assert len(tokens) == len(set(tokens)) == FULLTEXT_MAX_TOKENS


def test_retrieval_uses_pagerank_only_to_break_equal_relation_weights():
    from types import SimpleNamespace

    from mnogobase.core.models import EntityRecord, RelationView
    from mnogobase.retrieval.graph import GraphRetriever
    from tests.fakes import FakeEmbedder

    relations = [
        RelationView(
            src_id="seed",
            src_name="Seed",
            dst_id=eid,
            dst_name=eid,
            predicate="uses",
            weight=weight,
        )
        for eid, weight in (("low", 5), ("hub", 5), ("strong", 9))
    ]
    graph = SimpleNamespace(
        fulltext_entities=lambda *a: [("seed", 1.0)],
        get_entity=lambda *a: EntityRecord(entity_id="seed", name="Seed", type="Concept"),
        neighborhood=lambda *a: list(relations),
        analysis_scores=lambda *a: {"seed": 1.0, "hub": 8.0, "low": 0.5, "strong": 0.1},
    )
    vectors = SimpleNamespace(search_entities=lambda *a, **kw: [])
    retriever = GraphRetriever(graph, vectors, FakeEmbedder(), GraphSettings(use_gds=True))
    items = retriever.retrieve("Seed", 3)
    assert [i.ref for i in items if i.kind == "relation"] == [
        "seed|uses|strong",
        "seed|uses|hub",
        "seed|uses|low",
    ]
    # A dirty/missing snapshot keeps the previous candidate order.
    graph.analysis_scores = lambda *a: {}
    assert [i.ref for i in retriever.retrieve("Seed", 3) if i.kind == "relation"] == [
        "seed|uses|low",
        "seed|uses|hub",
        "seed|uses|strong",
    ]

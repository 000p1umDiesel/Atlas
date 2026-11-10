import os
import shutil
from pathlib import Path

import pytest

from mnogobase.config import GraphAnalysisSettings, Settings
from mnogobase.database import database_target, mark_project
from mnogobase.models import EntityRecord
from mnogobase.stores.graph_store import GraphStore
from tests.integration.graph_seed import CHUNKS, DOC_ID, populate

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def plugin_container(tmp_path_factory):
    from testcontainers.community.neo4j import Neo4jContainer

    container = Neo4jContainer("neo4j:5.26-community", password="testpassword")
    if jar := os.environ.get("MNOGOBASE_TEST_GDS_JAR"):
        # The official entrypoint chowns /plugins. Only a disposable copy is writable.
        plugins = tmp_path_factory.mktemp("neo4j-plugin-jars")
        shutil.copyfile(Path(jar).resolve(strict=True), plugins / "graph-data-science.jar")
        container.with_volume_mapping(str(plugins), "/plugins", "rw")
        container.with_env("NEO4J_PLUGINS", '["apoc"]')
    else:
        container.with_env("NEO4J_PLUGINS", '["apoc", "graph-data-science"]')
    container.with_env("NEO4J_dbms_security_procedures_unrestricted", "apoc.*,gds.*")
    container.with_env("NEO4J_server_memory_heap_max__size", "1G")
    container.with_env("NEO4J_server_memory_pagecache_size", "256M")
    container.with_kwargs(mem_limit="2g", nano_cpus=2_000_000_000)
    try:
        container.start()
        yield container
    finally:
        path = tmp_path_factory.getbasetemp() / "neo4j-plugins.log"
        stdout, stderr = container.get_logs()
        path.write_bytes(stdout + stderr)
        print(f"Neo4j plugin startup log: {path}")
        container.stop()


@pytest.fixture
def plugin_store(plugin_container):
    store = GraphStore(plugin_container.get_driver())
    store.wipe()
    store.ensure_schema()
    try:
        yield store
    finally:
        store.wipe()
        store.close()


def test_apoc_neighborhood_matches_cypher(plugin_store):
    populate(plugin_store, "Legacy")
    relations = plugin_store.neighborhood_apoc(["e1"], 3, 30, max_nodes=20, timeout=5)
    assert len(relations) == 1
    assert relations[0].description == "Legacy"
    assert set(relations[0].evidence) == set(CHUNKS)
    # Graph-size cap selects the ordinary Cypher path.
    assert plugin_store.neighborhood_apoc(["e1"], 3, 30, max_nodes=1) == plugin_store.neighborhood(
        ["e1"], 3, 30
    )


def test_plugins_analysis_and_deletion(plugin_store):
    from mnogobase.graph_analysis import analyze

    store = plugin_store
    populate(store, "Alpha")
    status = store.plugin_status()
    assert status["apoc"] and status["gds"]
    assert status["missing"] == []
    # A hub has greater PageRank than leaves in this undirected star.
    for i in range(3, 7):
        eid = f"e{i}"
        store.upsert_entity(EntityRecord(entity_id=eid, name=eid, type="Concept"))
        store.add_mentions(CHUNKS[0], [eid])
        store.merge_relation("e1", eid, "uses", "edge", 5, CHUNKS[0])
    preview = analyze(store, GraphAnalysisSettings(), estimate_only=True)
    assert preview["nodes"] == 6 and preview["projection_bytes"] > 0
    assert store.analysis_scores(["e1"]) == {}
    report = analyze(store, GraphAnalysisSettings())
    assert report["nodes"] == 6 and report["communities"] >= 1
    scores = store.analysis_scores([f"e{i}" for i in range(1, 7)])
    assert len(scores) == 6
    assert scores["e1"] > scores["e3"]
    first = {row["entity_id"]: row["community"] for row in store.analysis_details()}
    analyze(store, GraphAnalysisSettings())
    assert {row["entity_id"]: row["community"] for row in store.analysis_details()} == first
    assert store.owned_projections() == []
    store.delete_document(DOC_ID)
    assert not store.analysis_status()["ready"]
    assert store.analysis_scores(["e1"]) == {}


def test_gds_rejects_budget_and_cleans_up_after_algorithm_failure(plugin_store, monkeypatch):
    from mnogobase.graph_analysis import AnalysisError, analyze

    store = plugin_store
    populate(store, "Legacy")
    with pytest.raises(AnalysisError, match="nodes"):
        analyze(store, GraphAnalysisSettings(max_nodes=1))
    assert store.owned_projections() == []

    def fail(*args, **kwargs):
        raise AnalysisError("injected algorithm failure")

    monkeypatch.setattr(store, "gds_pagerank", fail)
    with pytest.raises(AnalysisError, match="injected"):
        analyze(store, GraphAnalysisSettings())
    assert store.owned_projections() == []
    assert store.analysis_scores(["e1"]) == {}


def test_empty_and_singleton_graphs(plugin_store):
    from mnogobase.graph_analysis import analyze

    store = plugin_store
    assert analyze(store, GraphAnalysisSettings())["nodes"] == 0
    store.upsert_entity(EntityRecord(entity_id="alone", name="Alone", type="Concept"))
    assert analyze(store, GraphAnalysisSettings())["communities"] == 1
    assert set(store.analysis_scores(["alone"])) == {"alone"}
    store.upsert_entity(EntityRecord(entity_id="another", name="Another", type="Concept"))
    assert analyze(store, GraphAnalysisSettings())["communities"] == 2


@pytest.mark.parametrize("weight", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_weights_are_rejected_before_projection(plugin_store, weight):
    from mnogobase.graph_analysis import AnalysisError, analyze

    store = plugin_store
    populate(store, "Legacy")
    store._run("MATCH (:Entity)-[r:RELATED]->(:Entity) SET r.weight = $weight", weight=weight)
    with pytest.raises(AnalysisError, match="positive finite"):
        analyze(store, GraphAnalysisSettings())
    assert store.owned_projections() == []


def test_changed_graph_cannot_publish_scores_from_old_projection(plugin_store, monkeypatch):
    from mnogobase.graph_analysis import AnalysisError, analyze

    store = plugin_store
    populate(store, "Legacy")
    original = store.gds_pagerank

    def concurrent_write(*args, **kwargs):
        rows = original(*args, **kwargs)
        store.upsert_entity(EntityRecord(entity_id="new", name="New", type="Concept"))
        return rows

    monkeypatch.setattr(store, "gds_pagerank", concurrent_write)
    with pytest.raises(AnalysisError, match="changed"):
        analyze(store, GraphAnalysisSettings())
    assert store.owned_projections() == []
    assert not store.analysis_status()["ready"]


def test_apoc_cycles_hops_and_original_citations(plugin_store):
    from qdrant_client import QdrantClient

    from mnogobase.config import GraphSettings
    from mnogobase.graph_analysis import analyze
    from mnogobase.retrieval.graph import GraphRetriever
    from mnogobase.stores.qdrant_store import QdrantStore
    from tests.fakes import FakeEmbedder

    store = plugin_store
    populate(store, "Alpha")
    for eid in ("e3", "e4", "e5"):
        store.upsert_entity(EntityRecord(entity_id=eid, name=eid, type="Concept"))
        store.add_mentions(CHUNKS[0], [eid])
    for src, dst in (("e2", "e3"), ("e3", "e1"), ("e3", "e4"), ("e4", "e5")):
        store.merge_relation(src, dst, "uses", "edge", 5, CHUNKS[0])
    one_hop = store.neighborhood_apoc(["e1"], 1, 30)
    assert len(one_hop) == 3
    assert {eid for r in one_hop for eid in (r.src_id, r.dst_id)} == {"e1", "e2", "e3"}
    assert len(store.neighborhood_apoc(["e1"], 3, 1)) == 1
    analyze(store, GraphAnalysisSettings())
    client = QdrantClient(":memory:")
    try:
        vectors = QdrantStore(client, "plugin_test_", 64)
        vectors.ensure_collections()
        retriever = GraphRetriever(
            store, vectors, FakeEmbedder(), GraphSettings(traversal="apoc", use_gds=True)
        )
        items = retriever.retrieve("Alpha", 5)
    finally:
        client.close()
    chunks = [item for item in items if item.kind == "chunk"]
    assert {item.ref for item in chunks} == set(CHUNKS)
    assert {item.path for item in chunks} == {"/Alpha.md"}
    assert any(item.kind == "relation" for item in items)


def test_graph_cli_works_without_model_servers(
    plugin_store, plugin_container, tmp_path, monkeypatch
):
    import json

    import yaml
    from typer.testing import CliRunner

    from mnogobase import cli
    from mnogobase.registry import Registry

    store = plugin_store
    populate(store, "Legacy")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TEST_NEO4J_PASSWORD", "testpassword")
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "database": {"backend": "postgresql"},
                "neo4j": {
                    "uri": plugin_container.get_connection_url(),
                    "password_env": "TEST_NEO4J_PASSWORD",
                },
            }
        )
    )
    Registry(database_target(Settings(), "state")).close()
    mark_project(Settings(data_dir=tmp_path / ".mnogobase"))
    monkeypatch.setattr(cli, "build_app", lambda *a: pytest.fail("model application created"))
    runner = CliRunner()
    for args in (["graph", "analyze", "--estimate"], ["graph", "analyze"], ["graph", "status"]):
        result = runner.invoke(cli.app, args)
        assert result.exit_code == 0, result.output
        report = json.loads(result.stdout)
        if args[-1] == "status":
            assert report["ready"] and len(report["entities"]) == 2
        else:
            assert report["nodes"] == 2 and report["plugins"]["gds"]
    result = runner.invoke(cli.app, ["graph", "clear", "--yes"])
    assert result.exit_code == 0, result.output
    assert not store.analysis_status()["ready"]
    assert store.counts()["Entity"] == 2

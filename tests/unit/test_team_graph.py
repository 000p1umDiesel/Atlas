from types import SimpleNamespace

import pytest

from mnogobase.stores.graph_store import GraphStore


@pytest.mark.parametrize(
    "team_id",
    ["", "a" * 31, "a" * 33, "A" * 32, "g" * 32, "a` MATCH (n) DELETE n", 12],
)
def test_graph_rejects_unvalidated_team_ids_before_database_access(team_id):
    with pytest.raises(ValueError, match="team_id"):
        GraphStore(None, team_id=team_id)


def test_from_settings_keeps_the_team_scope(monkeypatch):
    class Driver:
        def execute_query(self, query, **kwargs):
            # A team document must never be looked up through the legacy label.
            assert ":Document" not in query
            assert "a" * 32 in query
            return [], None, None

    monkeypatch.setattr(
        "mnogobase.stores.graph_store.GraphDatabase.driver", lambda *a, **kw: Driver()
    )
    settings = SimpleNamespace(
        uri="bolt://unused", user="neo4j", password=lambda: "unused", team_id="a" * 32
    )
    store = GraphStore.from_settings(settings)
    assert store.document_path("shared") is None

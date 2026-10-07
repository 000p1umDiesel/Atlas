from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest
from qdrant_client import QdrantClient

if TYPE_CHECKING:
    from testcontainers.community.neo4j import Neo4jContainer

    from mnogobase.stores.graph_store import GraphStore

QDRANT_IMAGE = "qdrant/qdrant:v1.19.2"


@pytest.fixture(scope="session")
def qdrant_client() -> Iterator[QdrantClient]:
    """Real Qdrant server in Docker. Integration tests only (`-m integration`)."""
    from testcontainers.community.qdrant import QdrantContainer

    with QdrantContainer(QDRANT_IMAGE) as container:
        client = container.get_client()
        try:
            yield client
        finally:
            client.close()


NEO4J_IMAGE = "neo4j:5.26-community"


@pytest.fixture(scope="session")
def neo4j_container() -> Iterator[Neo4jContainer]:
    """Real Neo4j server in Docker. Integration tests only (`-m integration`)."""
    from testcontainers.community.neo4j import Neo4jContainer

    with Neo4jContainer(NEO4J_IMAGE, password="testpassword") as container:
        yield container


@pytest.fixture
def graph(neo4j_container: Neo4jContainer) -> Iterator[GraphStore]:
    """Fresh `GraphStore` on the shared container: wiped, schema ensured."""
    from mnogobase.stores.graph_store import GraphStore

    store = GraphStore(neo4j_container.get_driver())
    store.wipe()
    store.ensure_schema()
    try:
        yield store
    finally:
        store.close()

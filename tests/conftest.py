from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest
from qdrant_client import QdrantClient

if TYPE_CHECKING:
    from testcontainers.community.neo4j import Neo4jContainer
    from testcontainers.community.qdrant import QdrantContainer

    from mnogobase.stores.graph_store import GraphStore

QDRANT_IMAGE = "qdrant/qdrant:v1.19.2"


@pytest.fixture(scope="session")
def qdrant_container() -> Iterator[QdrantContainer]:
    """Настоящий сервер Qdrant в Docker. Только для интеграционных тестов (`-m integration`)."""
    from testcontainers.community.qdrant import QdrantContainer

    with QdrantContainer(QDRANT_IMAGE) as container:
        yield container


@pytest.fixture(scope="session")
def qdrant_url(qdrant_container: QdrantContainer) -> str:
    """REST URL контейнера Qdrant для кода, который подключается по настройкам."""
    return f"http://{qdrant_container.rest_host_address}"


@pytest.fixture(scope="session")
def qdrant_client(qdrant_container: QdrantContainer) -> Iterator[QdrantClient]:
    client = qdrant_container.get_client()
    try:
        yield client
    finally:
        client.close()


NEO4J_IMAGE = "neo4j:5.26-community"


@pytest.fixture(scope="session")
def neo4j_container() -> Iterator[Neo4jContainer]:
    """Настоящий сервер Neo4j в Docker. Только для интеграционных тестов (`-m integration`)."""
    from testcontainers.community.neo4j import Neo4jContainer

    with Neo4jContainer(NEO4J_IMAGE, password="testpassword") as container:
        yield container


@pytest.fixture
def graph(neo4j_container: Neo4jContainer) -> Iterator[GraphStore]:
    """Чистый `GraphStore` на общем контейнере: данные стёрты, схема создана."""
    from mnogobase.stores.graph_store import DRIVER_OPTIONS, GraphStore

    store = GraphStore(neo4j_container.get_driver(**DRIVER_OPTIONS))
    store.wipe()
    store.ensure_schema()
    try:
        yield store
    finally:
        store.close()

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest
from qdrant_client import QdrantClient

from mnogobase.config import Settings
from mnogobase.database import connect, database_target

if TYPE_CHECKING:
    from testcontainers.community.neo4j import Neo4jContainer
    from testcontainers.community.qdrant import QdrantContainer

    from mnogobase.stores.graph_store import GraphStore

QDRANT_IMAGE = "qdrant/qdrant:v1.19.2"


@pytest.fixture(autouse=True)
def explicit_legacy_database(monkeypatch):
    """Offline tests explicitly use SQLite; PostgreSQL tests override Settings directly."""
    monkeypatch.setenv("MNOGOBASE_DATABASE__BACKEND", "sqlite")


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


@pytest.fixture
def pg_settings(tmp_path, monkeypatch, postgresql_url):
    monkeypatch.setenv("DATABASE_URL", postgresql_url)
    prefix = "test_" + uuid.uuid4().hex[:10]
    settings = Settings(
        data_dir=tmp_path / "state",
        service={"root": tmp_path / "service"},
        wiki={"dir": tmp_path / "wiki"},
        database={"backend": "postgresql", "schema_prefix": prefix},
    )
    yield settings
    connection = connect(database_target(settings))
    try:
        schemas = connection.execute("SELECT nspname FROM pg_namespace").fetchall()
        for (name,) in schemas:
            if name.startswith(prefix + "_"):
                connection.execute(f'DROP SCHEMA "{name}" CASCADE')
    finally:
        connection.close()


@pytest.fixture(scope="session")
def postgresql_url():
    """Изолированный PostgreSQL; внешний сервер разрешён только явным test URL."""
    if explicit := os.environ.get("MNOGOBASE_TEST_DATABASE_URL"):
        yield explicit
        return
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:18", driver="psycopg") as container:
        yield container.get_connection_url()

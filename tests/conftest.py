from __future__ import annotations

from collections.abc import Iterator

import pytest
from qdrant_client import QdrantClient

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

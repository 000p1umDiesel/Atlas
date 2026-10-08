from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

import httpx
from qdrant_client import QdrantClient
from qdrant_client import models as qm
from qdrant_client.http.exceptions import ResponseHandlingException
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from mnogobase.config import QdrantSettings
from mnogobase.ids import point_id
from mnogobase.models import ChunkRecord, EntityRecord, SearchHit

if TYPE_CHECKING:
    from mnogobase.wiki.render import Section

DENSE = "dense"
SPARSE = "bm25"
_KEYWORD = qm.PayloadSchemaType.KEYWORD
_TRANSPORT_ERRORS = (ResponseHandlingException, httpx.TransportError)
_DEFAULT_RETRY_WAIT = wait_exponential(multiplier=0.5, max=10)
QueryVectors = tuple[list[float], qm.SparseVector]  # (dense, sparse) of one query


class DimensionMismatchError(RuntimeError):
    pass


def _match(key: str, value: str) -> qm.Filter:
    return qm.Filter(must=[qm.FieldCondition(key=key, match=qm.MatchValue(value=value))])


class QdrantStore:
    def __init__(
        self, client: QdrantClient, prefix: str, dim: int, *, retry_wait=_DEFAULT_RETRY_WAIT
    ):
        self.client = client
        self.dim = dim
        self.chunks = f"{prefix}chunks"
        self.entities = f"{prefix}entities"
        self.wiki = f"{prefix}wiki_pages"
        self._retry_wait = retry_wait

    @classmethod
    def from_settings(cls, settings: QdrantSettings, dim: int) -> QdrantStore:
        return cls(QdrantClient(url=settings.url, timeout=60), settings.prefix, dim)

    def _call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run a Qdrant network call, retrying transport errors only."""
        retrying = Retrying(
            retry=retry_if_exception_type(_TRANSPORT_ERRORS),
            stop=stop_after_attempt(5),
            wait=self._retry_wait,
            reraise=True,
        )
        return retrying(fn, *args, **kwargs)

    # ---- collections ----
    def ensure_collections(self) -> None:
        self._ensure(
            self.chunks, sparse=True, indexes=("chunk_id", "doc_id", "entity_ids", "modality")
        )
        self._ensure(self.entities, sparse=False, indexes=("entity_id", "type"))
        self._ensure(self.wiki, sparse=True, indexes=("page_id", "entity_id"))

    def _ensure(self, name: str, *, sparse: bool, indexes: tuple[str, ...]) -> None:
        if self.client.collection_exists(name):
            existing = self.collection_dim(name)
            if existing != self.dim:
                raise DimensionMismatchError(
                    f"collection {name} has dim {existing}, the embedder produces {self.dim}; "
                    "run `mnogobase reindex`"
                )
            return
        self.client.create_collection(
            name,
            vectors_config={DENSE: qm.VectorParams(size=self.dim, distance=qm.Distance.COSINE)},
            sparse_vectors_config=(
                {SPARSE: qm.SparseVectorParams(modifier=qm.Modifier.IDF)} if sparse else None
            ),
        )
        for field in indexes:
            self.client.create_payload_index(name, field, field_schema=_KEYWORD)

    def collection_dim(self, name: str) -> int | None:
        if not self.client.collection_exists(name):
            return None
        vectors = self.client.get_collection(name).config.params.vectors
        return vectors[DENSE].size

    def drop_collections(self) -> None:
        for name in (self.chunks, self.entities, self.wiki):
            if self.client.collection_exists(name):
                self.client.delete_collection(name)

    def _upsert(self, name: str, points: list[qm.PointStruct], batch: int = 256) -> None:
        for start in range(0, len(points), batch):
            self._call(self.client.upsert, name, points[start : start + batch], wait=True)

    def _query(self, name: str, *, key: str, **kwargs: Any) -> list[SearchHit]:
        response = self._call(self.client.query_points, name, with_payload=True, **kwargs)
        return [
            SearchHit(key=p.payload[key], score=p.score, payload=p.payload) for p in response.points
        ]

    def _hybrid(
        self,
        name: str,
        dense: list[float],
        sparse: qm.SparseVector,
        k: int,
        key: str,
        extra: Sequence[QueryVectors] = (),
    ) -> list[SearchHit]:
        """Dense + BM25 prefetch per query (the main one and `extra`), all fused by RRF."""
        prefetch = [
            p
            for d, s in [(dense, sparse), *extra]
            for p in (
                qm.Prefetch(query=d, using=DENSE, limit=k * 4),
                qm.Prefetch(query=s, using=SPARSE, limit=k * 4),
            )
        ]
        return self._query(
            name,
            key=key,
            prefetch=prefetch,
            query=qm.FusionQuery(fusion=qm.Fusion.RRF),
            limit=k,
        )

    def _delete_where(self, name: str, key: str, value: str) -> None:
        self._call(
            self.client.delete, name, points_selector=qm.FilterSelector(filter=_match(key, value))
        )

    # ---- chunks ----
    def upsert_chunks(
        self,
        chunks: list[ChunkRecord],
        dense: list[list[float]],
        sparse: list[qm.SparseVector],
    ) -> None:
        points = [
            qm.PointStruct(
                id=point_id(c.chunk_id),
                vector={DENSE: d, SPARSE: s},
                payload={
                    "chunk_id": c.chunk_id,
                    "doc_id": c.doc_id,
                    "text": c.text,
                    "headings": c.headings,
                    "page": c.page_start,
                    "path": c.path,
                    "modality": c.modality,
                    "entity_ids": [],
                },
            )
            for c, d, s in zip(chunks, dense, sparse, strict=True)
        ]
        self._upsert(self.chunks, points)

    def get_chunks(self, chunk_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Payloads of those of `chunk_ids` that exist, keyed by chunk id."""
        if not chunk_ids:
            return {}
        points = self._call(
            self.client.retrieve,
            self.chunks,
            ids=[point_id(c) for c in chunk_ids],
            with_payload=True,
            with_vectors=False,
        )
        return {p.payload["chunk_id"]: p.payload for p in points}

    def set_chunk_entities(self, chunk_id: str, entity_ids: list[str]) -> None:
        self._call(
            self.client.set_payload,
            self.chunks,
            {"entity_ids": sorted(set(entity_ids))},
            points=[point_id(chunk_id)],
        )

    def set_doc_path(self, doc_id: str, path: str) -> None:
        """Point every chunk of a document at another file path (used for citations)."""
        self._call(
            self.client.set_payload, self.chunks, {"path": path}, points=_match("doc_id", doc_id)
        )

    def delete_doc(self, doc_id: str) -> None:
        self._delete_where(self.chunks, "doc_id", doc_id)

    def search_chunks(
        self,
        dense: list[float],
        sparse: qm.SparseVector,
        k: int,
        extra: Sequence[QueryVectors] = (),
    ) -> list[SearchHit]:
        return self._hybrid(self.chunks, dense, sparse, k, key="chunk_id", extra=extra)

    def search_chunks_for_entity(
        self, dense: list[float], entity_id: str, k: int
    ) -> list[SearchHit]:
        return self._query(
            self.chunks,
            key="chunk_id",
            query=dense,
            using=DENSE,
            query_filter=_match("entity_ids", entity_id),
            limit=k,
        )

    # ---- entities ----
    def upsert_entities(self, entities: list[EntityRecord], dense: list[list[float]]) -> None:
        points = [
            qm.PointStruct(
                id=point_id(e.entity_id),
                vector={DENSE: d},
                payload={"entity_id": e.entity_id, "name": e.name, "type": e.type},
            )
            for e, d in zip(entities, dense, strict=True)
        ]
        self._upsert(self.entities, points)

    def search_entities(
        self, dense: list[float], k: int, score_threshold: float | None = None
    ) -> list[SearchHit]:
        return self._query(
            self.entities,
            key="entity_id",
            query=dense,
            using=DENSE,
            limit=k,
            score_threshold=score_threshold,
        )

    def delete_entities(self, entity_ids: list[str]) -> None:
        if entity_ids:
            self._call(
                self.client.delete,
                self.entities,
                points_selector=qm.PointIdsList(points=[point_id(e) for e in entity_ids]),
            )

    # ---- wiki ----
    def upsert_wiki_sections(
        self,
        page_id: str,
        entity_id: str,
        path: str,
        sections: list[Section],
        dense: list[list[float]],
        sparse: list[qm.SparseVector],
    ) -> None:
        self.delete_wiki_page(page_id)
        points = []
        for i, (section, d, s) in enumerate(zip(sections, dense, sparse, strict=True)):
            key = f"{page_id}#{i}"
            points.append(
                qm.PointStruct(
                    id=point_id(key),
                    vector={DENSE: d, SPARSE: s},
                    payload={
                        "key": key,
                        "page_id": page_id,
                        "entity_id": entity_id,
                        "path": path,
                        "section": section.name,
                        "text": section.text,
                        "chunk_ids": section.chunk_ids,
                    },
                )
            )
        self._upsert(self.wiki, points)

    def delete_wiki_page(self, page_id: str) -> None:
        self._delete_where(self.wiki, "page_id", page_id)

    def search_wiki(
        self,
        dense: list[float],
        sparse: qm.SparseVector,
        k: int,
        extra: Sequence[QueryVectors] = (),
    ) -> list[SearchHit]:
        return self._hybrid(self.wiki, dense, sparse, k, key="key", extra=extra)

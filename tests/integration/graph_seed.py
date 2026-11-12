"""Небольшой граф для интеграционных тестов: документ, два чанка, две сущности, wiki."""

from mnogobase.core.models import ChunkRecord, DocumentRecord, EntityRecord, WikiPageRecord

DOC_ID = "d" * 16
CHUNKS = [f"{DOC_ID}:{i:05d}" for i in range(2)]


def add_mentions(store, chunk_id: str, entity_ids: list[str]) -> None:
    """Упоминания сущностей чанком без связей (обёртка над `add_chunk_facts`)."""
    store.add_chunk_facts(chunk_id, entity_ids, [])


def merge_relation(store, src_id, dst_id, predicate, description, strength, chunk_id) -> None:
    """Одна связь `src -> dst`, подтверждённая чанком `chunk_id`."""
    relation = {
        "src": src_id,
        "dst": dst_id,
        "predicate": predicate,
        "description": description,
        "strength": strength,
    }
    store.add_chunk_facts(chunk_id, [], [relation])


def populate(store, title):
    store.upsert_document(
        DocumentRecord(doc_id=DOC_ID, path=f"/{title}.md", title=title, mime="text/markdown")
    )
    store.upsert_chunks(
        [
            ChunkRecord(
                chunk_id=cid,
                doc_id=DOC_ID,
                idx=i,
                text=f"{title} body {i}",
                context_text=f"{title} body {i}",
            )
            for i, cid in enumerate(CHUNKS)
        ]
    )
    for eid in ("e1", "e2"):
        store.upsert_entity(EntityRecord(entity_id=eid, name=f"{title} {eid}", type="Concept"))
        add_mentions(store, CHUNKS[0], [eid])
        store.upsert_wiki_page(
            WikiPageRecord(page_id=eid, slug=eid, title=title, path=f"{title}/{eid}.md"),
            eid,
            [],
            [CHUNKS[0]],
        )
    store.upsert_wiki_page(store.wiki_page("e1"), "e1", ["e2"], [CHUNKS[1]])
    merge_relation(store, "e1", "e2", "uses", title, 3, CHUNKS[0])
    merge_relation(store, "e1", "e2", "uses", title, 2, CHUNKS[1])

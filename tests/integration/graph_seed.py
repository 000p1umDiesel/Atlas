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


# ---- граф для представлений узлов, каталога сущностей, обзора, путей и сообществ ----
VIEW_DOC_A = "a" * 16
VIEW_DOC_B = "b" * 16
# (entity_id, имя, тип, алиасы, чанки с упоминаниями)
VIEW_ENTITIES = [
    ("ada", "Ada Lovelace", "Person", ["Lovelace"], ["A0", "A1", "B0"]),
    ("babbage", "Charles Babbage", "Person", [], ["A0", "B1"]),
    ("engine", "Analytical Engine", "Equipment", [], ["A1", "A2"]),
    ("notes", "Notes on the Engine", "Work", [], ["A2"]),
    ("loom", "Jacquard Loom", "Equipment", [], ["B1"]),
    ("lonely", "Lonely Idea", "Concept", [], ["B0"]),
]
# (источник, предикат, цель, сила, чанк)
VIEW_RELATIONS = [
    ("ada", "collaborated_with", "babbage", 3, "A0"),
    ("ada", "collaborated_with", "babbage", 3, "B0"),
    ("ada", "wrote", "notes", 5, "A2"),
    ("babbage", "designed", "engine", 8, "A1"),
    ("ada", "described", "engine", 4, "A1"),
    ("engine", "inspired_by", "loom", 2, "B1"),
]
# entity_id -> (PageRank, сообщество) для `publish_views_analysis`
VIEW_ANALYSIS = {
    "ada": (0.30, "community:x"),
    "babbage": (0.25, "community:x"),
    "engine": (0.20, "community:y"),
    "notes": (0.10, "community:x"),
    "loom": (0.10, "community:y"),
    "lonely": (0.05, "community:z"),
}


def view_chunk(label: str) -> str:
    """`chunk_id` по метке вида `A0` / `B1`: документ VIEW_DOC_A или VIEW_DOC_B и номер."""
    doc_id = VIEW_DOC_A if label[0] == "A" else VIEW_DOC_B
    return f"{doc_id}:{int(label[1:]):05d}"


def populate_views(store) -> None:
    """Два документа, шесть сущностей, пять связей и две wiki-страницы (у `babbage` —
    устаревшая). Степени RELATED: ada 3, babbage 2, engine 3, notes 1, loom 1, lonely 0.
    Аналитику публикует отдельно `publish_views_analysis` (любая запись её сбрасывает)."""
    for doc_id, name, n in ((VIEW_DOC_A, "alpha", 3), (VIEW_DOC_B, "beta", 2)):
        store.upsert_document(
            DocumentRecord(doc_id=doc_id, path=f"/{name}.md", title=name, mime="text/markdown")
        )
        store.upsert_chunks(
            [
                ChunkRecord(
                    chunk_id=f"{doc_id}:{i:05d}",
                    doc_id=doc_id,
                    idx=i,
                    text=f"{name} body {i}",
                    context_text=f"{name} body {i}",
                )
                for i in range(n)
            ]
        )
    store.upsert_entities(
        [
            EntityRecord(entity_id=eid, name=name, type=etype, aliases=aliases)
            for eid, name, etype, aliases, _ in VIEW_ENTITIES
        ]
    )
    mentions: dict[str, list[str]] = {}
    for eid, *_, labels in VIEW_ENTITIES:
        for label in labels:
            mentions.setdefault(label, []).append(eid)
    for label, eids in mentions.items():
        add_mentions(store, view_chunk(label), eids)
    for src, predicate, dst, strength, label in VIEW_RELATIONS:
        merge_relation(
            store, src, dst, predicate, f"{src} {predicate} {dst}", strength, view_chunk(label)
        )
    for eid, slug, title in (
        ("ada", "ada-lovelace", "Ada Lovelace"),
        ("babbage", "charles-babbage", "Charles Babbage"),
    ):
        store.upsert_wiki_page(
            WikiPageRecord(page_id=eid, slug=slug, title=title, path=f"entities/{slug}.md"),
            eid,
            [],
            [view_chunk("A0")],
        )
    store.invalidate_wiki_page("babbage")


def publish_views_analysis(store) -> None:
    """Публикует PageRank и сообщества `VIEW_ANALYSIS` как актуальный запуск аналитики."""
    rows = [
        {"entity_id": eid, "pagerank": pagerank, "community": community}
        for eid, (pagerank, community) in VIEW_ANALYSIS.items()
    ]
    assert store.publish_analysis(store.analysis_status()["revision"], rows, {})

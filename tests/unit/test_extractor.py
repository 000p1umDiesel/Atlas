from mnogobase.config import DEFAULT_ENTITY_TYPES
from mnogobase.extraction.extractor import (
    PROMPT_VERSION,
    Extractor,
    clean_extraction,
    entity_types_signature,
    format_entity_types,
    stale_entity_types,
)
from mnogobase.models import ChunkRecord, ExtractedEntity, ExtractedRelation, ExtractionResult
from mnogobase.registry import Registry
from tests.fakes import FakeLLM, RecordingProgress, scripted_llm_handler

DOC = "a" * 16


def chunk(text: str, idx: int = 0, doc: str = DOC) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=f"{doc}:{idx:05d}",
        doc_id=doc,
        idx=idx,
        text=text,
        context_text=text,
        headings=["Intro"],
    )


def E(name, etype="Concept", aliases=()):  # noqa: N802
    return ExtractedEntity(name=name, type=etype, description=f"{name}.", aliases=list(aliases))


def R(source, target, predicate="uses", strength=5):  # noqa: N802
    return ExtractedRelation(
        source=source, target=target, predicate=predicate, description="d", strength=strength
    )


async def test_extract_uses_cache(tmp_path):
    reg = Registry(tmp_path / "s.db")
    llm = FakeLLM(scripted_llm_handler)
    ex = Extractor(llm, reg, DEFAULT_ENTITY_TYPES)
    c = chunk("The Transformer uses softmax.")
    first = await ex.extract(c, "Doc")
    second = await ex.extract(c, "Doc")
    assert [e.name for e in first.entities] == ["Transformer", "Softmax"]
    assert second == first
    assert ex.cached(c) == first
    prompts = llm.calls_for("extract")
    assert len(prompts) == 1
    assert "Document: Doc" in prompts[0] and "Section: Intro" in prompts[0]
    assert f"- Person: {DEFAULT_ENTITY_TYPES['Person']}\n" in prompts[0]
    assert f"- Other: {DEFAULT_ENTITY_TYPES['Other']}" in prompts[0]


def test_clean_extraction_relations():
    raw = ExtractionResult(
        entities=[
            E("Transformer", "method", aliases=["Трансформер"]),
            E("Softmax", "Weird"),
            E("   "),
            E("transformer", "Method", aliases=["TF"]),
        ],
        relations=[
            R("Трансформер", "Softmax", predicate="Uses It", strength=42),
            R("Transformer", "Unknown"),
            R("Softmax", "Softmax"),
            R("Transformer", "Softmax", predicate="uses it"),
        ],
    )
    out = clean_extraction(raw, DEFAULT_ENTITY_TYPES)
    assert [e.name for e in out.entities] == ["Transformer", "Softmax"]
    assert out.entities[0].type == "Method"
    assert out.entities[1].type == "Other"
    assert sorted(out.entities[0].aliases) == ["TF", "Трансформер"]
    assert len(out.relations) == 1
    rel = out.relations[0]
    assert (rel.source, rel.target, rel.predicate, rel.strength) == (
        "Transformer",
        "Softmax",
        "uses_it",
        10,
    )


async def test_extract_many_isolates_failures(tmp_path):
    reg = Registry(tmp_path / "s.db")

    def handler(task, prompt):
        if "BROKEN" in prompt:
            raise RuntimeError("llm down")
        return scripted_llm_handler(task, prompt)

    ex = Extractor(FakeLLM(handler), reg, DEFAULT_ENTITY_TYPES)
    ok, bad = chunk("Transformer text", 0), chunk("BROKEN", 1)
    results = await ex.extract_many([ok, bad], "Doc")
    assert list(results) == [ok.chunk_id]
    assert reg.chunk_extract_counts(DOC) == {"done": 1, "failed": 1}


async def test_extract_many_advances_once_per_chunk_with_cache_hits_and_failures(tmp_path):
    reg = Registry(tmp_path / "s.db")

    def handler(task, prompt):
        if "BROKEN" in prompt:
            raise RuntimeError("llm down")
        return scripted_llm_handler(task, prompt)

    llm = FakeLLM(handler)
    ex = Extractor(llm, reg, DEFAULT_ENTITY_TYPES)
    cached, fresh, bad = chunk("Softmax text", 0), chunk("Transformer text", 1), chunk("BROKEN", 2)
    await ex.extract(cached, "Doc")
    progress = RecordingProgress()
    results = await ex.extract_many([cached, fresh, bad], "Doc", progress=progress)
    assert set(results) == {cached.chunk_id, fresh.chunk_id}
    assert sorted(progress.events) == [
        ("advance", 1, False),
        ("advance", 1, False),
        ("advance", 1, True),
    ]
    assert len(llm.calls_for("extract")) == 3  # попадание в кэш обошлось без вызова


async def test_cached_falls_back_to_another_model(tmp_path):
    reg = Registry(tmp_path / "s.db")
    llm = FakeLLM(scripted_llm_handler)
    ex = Extractor(llm, reg, DEFAULT_ENTITY_TYPES)
    c = chunk("The Transformer uses softmax.")
    first = await ex.extract(c, "Doc")
    llm.model_for = lambda task: "another-model"
    assert ex.cached(c) is None  # точный ключ: модель изменилась
    assert ex.cached(c, any_version=True) == first
    assert ex.cached(chunk("never extracted", idx=1), any_version=True) is None


GENES = {"Gene": "A named gene.", "Other": "Anything else."}


def test_prompt_lists_types_with_descriptions():
    assert format_entity_types({"Gene": "A named gene.", "Bare": "", "Other": "Else."}) == (
        "- Gene: A named gene.\n- Bare\n- Other: Else."
    )


async def test_prompt_has_no_hard_coded_kinds(tmp_path):
    llm = FakeLLM(scripted_llm_handler)
    ex = Extractor(llm, Registry(tmp_path / "s.db"), GENES)
    await ex.extract(chunk("The Transformer uses softmax."), "Doc")
    [prompt] = llm.calls_for("extract")
    assert "- Gene: A named gene.\n- Other: Anything else." in prompt
    assert "people, organizations" not in prompt and "Person" not in prompt


def test_types_signature_is_short_stable_and_covers_descriptions():
    sig = entity_types_signature(GENES)
    assert sig == entity_types_signature(dict(GENES)) and len(sig) == 12
    assert sig != entity_types_signature({"Gene": "A gene.", "Other": "Anything else."})
    assert sig != entity_types_signature({"Other": "Anything else.", "Gene": "A named gene."})


async def test_cache_is_keyed_by_the_entity_types(tmp_path):
    reg = Registry(tmp_path / "s.db")
    llm = FakeLLM(scripted_llm_handler)
    c = chunk("The Transformer uses softmax.")
    await Extractor(llm, reg, DEFAULT_ENTITY_TYPES).extract(c, "Doc")
    await Extractor(llm, reg, dict(DEFAULT_ENTITY_TYPES)).extract(c, "Doc")
    assert len(llm.calls_for("extract")) == 1  # те же типы: попадание в кэш
    changed = {**DEFAULT_ENTITY_TYPES, "Method": "A changed description."}
    again = Extractor(llm, reg, changed)
    assert again.cached(c) is None
    await again.extract(c, "Doc")
    assert len(llm.calls_for("extract")) == 2  # типы изменились: извлечено заново


async def test_cache_hits_are_recleaned_with_the_current_types(tmp_path):
    reg = Registry(tmp_path / "s.db")
    llm = FakeLLM(scripted_llm_handler)
    c = chunk("The Transformer uses softmax.")
    await Extractor(llm, reg, DEFAULT_ENTITY_TYPES).extract(c, "Doc")
    genes = Extractor(llm, reg, GENES)
    fallback = genes.cached(c, any_version=True)  # извлечено с другими типами
    assert [e.type for e in fallback.entities] == ["Other", "Other"]
    assert len(fallback.relations) == 1
    # попадание по точному ключу тоже очищается заново
    exact = ExtractionResult(entities=[E("Transformer", "Method")], relations=[])
    reg.put_extraction(c.chunk_id, genes._version, genes.model, exact.model_dump_json())
    assert [e.type for e in genes.cached(c).entities] == ["Other"]


def test_empty_cache_is_never_stale(tmp_path):
    assert not stale_entity_types(Registry(tmp_path / "s.db"), DEFAULT_ENTITY_TYPES)


async def test_stale_until_every_document_is_extracted_with_the_current_types(tmp_path):
    reg = Registry(tmp_path / "s.db")
    llm = FakeLLM(scripted_llm_handler)
    doc_a, doc_b, doc_a2, doc_b2 = "a" * 16, "b" * 16, "c" * 16, "d" * 16
    old = Extractor(llm, reg, DEFAULT_ENTITY_TYPES)
    await old.extract(chunk("The Transformer uses softmax.", doc=doc_a), "A")
    await old.extract(chunk("Vaswani wrote it.", doc=doc_b), "B")
    assert not stale_entity_types(reg, DEFAULT_ENTITY_TYPES)
    # конфиг изменён, заново ещё ничего не извлекали: у старых документов старые типы
    assert stale_entity_types(reg, GENES)

    # оба документа изменены (новые doc_ids) и заново извлечены с новыми типами
    new = Extractor(llm, reg, GENES)
    reg.clear_doc(doc_a)
    await new.extract(chunk("The Transformer uses softmax again.", doc=doc_a2), "A")
    assert stale_entity_types(reg, GENES)  # у B всё ещё старые типы
    reg.clear_doc(doc_b)
    await new.extract(chunk("Vaswani wrote it again.", doc=doc_b2), "B")
    assert not stale_entity_types(reg, GENES)
    assert stale_entity_types(reg, DEFAULT_ENTITY_TYPES)


async def test_a_chunk_re_extracted_with_the_current_types_is_not_stale(tmp_path):
    reg = Registry(tmp_path / "s.db")
    llm = FakeLLM(scripted_llm_handler)
    c = chunk("The Transformer uses softmax.")
    await Extractor(llm, reg, DEFAULT_ENTITY_TYPES).extract(c, "Doc")
    await Extractor(llm, reg, GENES).extract(c, "Doc")  # например, --retry-failed для extract
    assert not stale_entity_types(reg, GENES)  # учитывается только последняя строка чанка


def test_legacy_extractions_are_stale(tmp_path):
    reg = Registry(tmp_path / "s.db")
    empty = ExtractionResult(entities=[], relations=[]).model_dump_json()
    reg.put_extraction(f"{DOC}:00000", "extract-v1", "fake-extract", empty)
    assert stale_entity_types(reg, DEFAULT_ENTITY_TYPES)  # создано до того, как типы вошли в ключ


def test_a_new_prompt_version_with_the_same_types_is_not_stale(tmp_path):
    reg = Registry(tmp_path / "s.db")
    empty = ExtractionResult(entities=[], relations=[]).model_dump_json()
    sig = entity_types_signature(DEFAULT_ENTITY_TYPES)
    reg.put_extraction(f"{DOC}:00000", f"extract-v99:{sig}", "m", empty)
    reg.put_extraction(f"{DOC}:00001", f"{PROMPT_VERSION}:{sig}", "m", empty)
    assert not stale_entity_types(reg, DEFAULT_ENTITY_TYPES)


async def test_cached_does_not_write(tmp_path):
    reg = Registry(tmp_path / "s.db")
    llm = FakeLLM(scripted_llm_handler)
    c = chunk("The Transformer uses softmax.")
    await Extractor(llm, reg, DEFAULT_ENTITY_TYPES).extract(c, "Doc")
    before = reg._db.execute("SELECT * FROM meta").fetchall()
    assert Extractor(llm, reg, GENES).cached(c, any_version=True) is not None
    assert reg._db.execute("SELECT * FROM meta").fetchall() == before

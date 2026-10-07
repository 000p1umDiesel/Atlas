from mnogobase.config import DEFAULT_ENTITY_TYPES
from mnogobase.extraction.extractor import (
    MIXED_TYPES,
    TYPES_META,
    Extractor,
    clean_extraction,
    entity_types_signature,
    format_entity_types,
    stale_entity_types,
)
from mnogobase.models import ChunkRecord, ExtractedEntity, ExtractedRelation, ExtractionResult
from mnogobase.registry import Registry
from tests.fakes import FakeLLM, scripted_llm_handler

DOC = "a" * 16


def chunk(text: str, idx: int = 0) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=f"{DOC}:{idx:05d}",
        doc_id=DOC,
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


async def test_cached_falls_back_to_another_model(tmp_path):
    reg = Registry(tmp_path / "s.db")
    llm = FakeLLM(scripted_llm_handler)
    ex = Extractor(llm, reg, DEFAULT_ENTITY_TYPES)
    c = chunk("The Transformer uses softmax.")
    first = await ex.extract(c, "Doc")
    llm.model_for = lambda task: "another-model"
    assert ex.cached(c) is None  # exact key: model changed
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
    assert len(llm.calls_for("extract")) == 1  # same types: cache hit
    changed = {**DEFAULT_ENTITY_TYPES, "Method": "A changed description."}
    again = Extractor(llm, reg, changed)
    assert again.cached(c) is None
    await again.extract(c, "Doc")
    assert len(llm.calls_for("extract")) == 2  # changed types: re-extracted


async def test_cache_hits_are_recleaned_with_the_current_types(tmp_path):
    reg = Registry(tmp_path / "s.db")
    llm = FakeLLM(scripted_llm_handler)
    c = chunk("The Transformer uses softmax.")
    await Extractor(llm, reg, DEFAULT_ENTITY_TYPES).extract(c, "Doc")
    genes = Extractor(llm, reg, GENES)
    fallback = genes.cached(c, any_version=True)  # extracted with other types
    assert [e.type for e in fallback.entities] == ["Other", "Other"]
    assert len(fallback.relations) == 1
    # an exact-key hit is re-cleaned too
    exact = ExtractionResult(entities=[E("Transformer", "Method")], relations=[])
    reg.put_extraction(c.chunk_id, genes._version, genes.model, exact.model_dump_json())
    assert [e.type for e in genes.cached(c).entities] == ["Other"]


async def test_types_meta_follows_what_was_extracted(tmp_path):
    reg = Registry(tmp_path / "s.db")
    llm = FakeLLM(scripted_llm_handler)
    first = Extractor(llm, reg, DEFAULT_ENTITY_TYPES)
    assert not stale_entity_types(reg, DEFAULT_ENTITY_TYPES)  # nothing extracted yet
    await first.extract(chunk("The Transformer uses softmax.", 0), "Doc")
    assert reg.get_meta(TYPES_META) == entity_types_signature(DEFAULT_ENTITY_TYPES)
    await first.extract(chunk("Vaswani wrote it.", 1), "Doc")
    assert not stale_entity_types(reg, DEFAULT_ENTITY_TYPES)
    # config changed, nothing re-extracted yet: the old documents keep the old types
    assert stale_entity_types(reg, GENES)
    assert reg.get_meta(TYPES_META) == entity_types_signature(DEFAULT_ENTITY_TYPES)
    # a new chunk extracted with the new types: the graph now mixes both sets
    await Extractor(llm, reg, GENES).extract(chunk("Attention text", 2), "Doc")
    assert reg.get_meta(TYPES_META) == MIXED_TYPES
    assert stale_entity_types(reg, GENES) and stale_entity_types(reg, DEFAULT_ENTITY_TYPES)
    # after a reset everything is extracted again with one set
    reg.wipe()
    await Extractor(llm, reg, GENES).extract(chunk("Attention text", 2), "Doc")
    assert not stale_entity_types(reg, GENES)


async def test_extractions_without_a_types_record_are_stale(tmp_path):
    reg = Registry(tmp_path / "s.db")
    reg.put_extraction(
        f"{DOC}:00000",
        "extract-v1",
        "fake-extract",
        ExtractionResult(entities=[], relations=[]).model_dump_json(),
    )
    assert stale_entity_types(reg, DEFAULT_ENTITY_TYPES)  # made before types were recorded
    await Extractor(FakeLLM(scripted_llm_handler), reg, DEFAULT_ENTITY_TYPES).extract(
        chunk("Softmax text", 1), "Doc"
    )
    assert reg.get_meta(TYPES_META) == MIXED_TYPES


async def test_graph_fallback_to_other_types_marks_them_mixed(tmp_path):
    reg = Registry(tmp_path / "s.db")
    llm = FakeLLM(scripted_llm_handler)
    c = chunk("The Transformer uses softmax.")
    await Extractor(llm, reg, DEFAULT_ENTITY_TYPES).extract(c, "Doc")
    llm.model_for = lambda task: "another-model"
    same_types = Extractor(llm, reg, DEFAULT_ENTITY_TYPES)
    assert same_types.cached(c, any_version=True) is not None
    assert reg.get_meta(TYPES_META) == entity_types_signature(DEFAULT_ENTITY_TYPES)
    assert Extractor(llm, reg, GENES).cached(c, any_version=True) is not None
    assert reg.get_meta(TYPES_META) == MIXED_TYPES

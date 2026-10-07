from mnogobase.config import DEFAULT_ENTITY_TYPES
from mnogobase.extraction.extractor import Extractor, clean_extraction
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
    assert "Person, Organization" in prompts[0]


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

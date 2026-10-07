from datetime import UTC, date, datetime

from mnogobase.models import EntityRecord, PageIndexRow, RelationView
from mnogobase.wiki.render import (
    Section,
    SourceRef,
    append_log,
    parse_page,
    render_index,
    render_page,
    split_sections,
)
from mnogobase.wiki.validate import (
    cited_ids,
    resolve_links,
    strip_reserved_sections,
    validate_citations,
)


def test_validate_citations():
    body = "A [^x:1]. B [^bad]. C [^x:1][^x:2]."
    text, cited = validate_citations(body, {"x:1", "x:2"})
    assert text == "A [^x:1]. B. C [^x:1][^x:2]."
    assert cited == ["x:1", "x:2"]


def test_validate_citations_keeps_newline_before_rejected_citation():
    text, cited = validate_citations("Line one.\n[^bad]Line two. [^x:1]", {"x:1"})
    assert text == "Line one.\nLine two. [^x:1]"
    assert cited == ["x:1"]


def test_resolve_links():
    pages = {
        "softmax": ("e2", "softmax", "Softmax"),
        "трансформер": ("e1", "transformer", "Transformer"),
    }
    text, linked = resolve_links("See [[Softmax]], [[Трансформер|the model]] and [[Nope]].", pages)
    assert text == "See [[softmax|Softmax]], [[transformer|the model]] and Nope."
    assert linked == ["e2", "e1"]


def test_resolve_links_special_characters():
    pages = {
        "c#": ("e5", "csharp", "C#"),
        "c++": ("e6", "c++", "C++"),
    }
    text, linked = resolve_links("Compare [[C#]] with [[C++|cpp]].", pages)
    assert text == "Compare [[csharp|C#]] with [[c++|cpp]]."
    assert linked == ["e5", "e6"]


def test_strip_reserved_sections():
    body = "# Title\nText [^a]\n\n## Details\nX\n\n## Related\n- foo\n\n## Sources\n[^a]: f"
    assert strip_reserved_sections(body) == "Text [^a]\n\n## Details\nX"
    assert strip_reserved_sections("Text\n[^a]: definition\nMore") == "Text\n\nMore"


def test_strip_reserved_sections_only_matches_exact_headings():
    body = "Text\n\n## Related work\nY\n\n## Sources of error\nZ\n\n## related  \n- foo"
    assert strip_reserved_sections(body) == "Text\n\n## Related work\nY\n\n## Sources of error\nZ"
    # the heading must sit on one line: a bare "##" line followed by "Related" is not one
    assert strip_reserved_sections("Text\n##\nRelated\nMore") == "Text\n##\nRelated\nMore"


def _page() -> str:
    entity = EntityRecord(entity_id="e1", name="Transformer", type="Method", aliases=["TF"])
    relations = [
        RelationView(
            src_id="e1",
            src_name="Transformer",
            predicate="related_to",
            dst_id="e2",
            dst_name="Softmax",
        ),
        RelationView(
            src_id="e3", src_name="Cat", predicate="uses", dst_id="e1", dst_name="Transformer"
        ),
    ]
    body = "Summary [^c1] about [[softmax|Softmax]].\n\n## Details\nMore [^c2][^c1]."
    return render_page(
        entity,
        body,
        relations,
        {"e2": "softmax"},
        [SourceRef("c1", "/docs/a.pdf", 3), SourceRef("c2", "/docs/b.pdf", None)],
        3,
        date(2026, 10, 7),
    )


def test_render_and_parse_roundtrip():
    text = _page()
    assert text.startswith("---\nid: e1\n")
    assert "version: 3" in text and "updated: '2026-10-07'" in text
    assert "# Transformer" in text
    assert "- related_to → [[softmax|Softmax]]" in text
    assert "- uses ← Cat" in text
    assert "[^c1]: *a.pdf*, p.3" in text
    assert "[^c2]: *b.pdf*\n" in text
    meta, body = parse_page(text)
    assert meta["version"] == 3 and meta["aliases"] == ["TF"]
    assert meta["updated"] == "2026-10-07" and meta["sources"] == 2
    assert body == "Summary [^c1] about [[softmax|Softmax]].\n\n## Details\nMore [^c2][^c1]."


def test_split_sections_for_embedding():
    sections = split_sections(_page())
    assert all(isinstance(s, Section) for s in sections)
    assert [s.name for s in sections] == ["Summary", "Details", "Related"]
    summary, details, related = sections
    assert summary.text.startswith("Transformer — Summary\n")
    assert "[^" not in summary.text and "about Softmax" in summary.text
    assert summary.chunk_ids == ["c1"]
    assert details.text == "Transformer — Details\nMore."
    assert details.chunk_ids == ["c2", "c1"]
    assert related.chunk_ids == []
    assert "related_to → Softmax" in related.text


def test_split_sections_without_title_or_sources():
    sections = split_sections("Lead [^d:00001] text [^d:00001].\n\n## Empty\n\n## Next\nBody")
    assert sections == [
        Section("Summary", "Lead text.", ["d:00001"]),
        Section("Next", "Body", []),
    ]


def test_index_and_log(tmp_path):
    rows = [
        PageIndexRow(
            page_id="e2",
            slug="softmax",
            title="Softmax",
            path="entities/softmax.md",
            entity_type="Concept",
        ),
        PageIndexRow(
            page_id="e1",
            slug="transformer",
            title="Transformer",
            path="entities/transformer.md",
            entity_type="Method",
        ),
    ]
    index = render_index(rows)
    assert "## Concept\n- [[softmax|Softmax]]" in index
    assert "## Method\n- [[transformer|Transformer]]" in index
    log = tmp_path / "wiki" / "log.md"
    now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    append_log(log, "r1", ["/docs/a.pdf", "/docs/b.pdf"], ["Transformer"], [], [], now=now)
    append_log(log, "r2", [], [], ["Transformer"], ["Cat"], now=now)
    append_log(log, "r3", [], [], [], [], now=now)
    text = log.read_text(encoding="utf-8")
    assert text.startswith("# Wiki Log\n")
    assert "## 2026-10-07 12:00:00 UTC · run r1" in text
    assert "- documents (2): /docs/a.pdf, /docs/b.pdf" in text
    assert "- created (1): Transformer" in text
    assert "- updated (1): Transformer" in text and "- deleted (1): Cat" in text
    assert text.count("- documents") == 1
    assert text.endswith("run r3\n- no changes\n")


def test_cited_ids_in_first_seen_order():
    body = "A [^d:00002] b [^d:00001]. Again [^d:00002].\n\nNo cite [x]."
    assert cited_ids(body) == ["d:00002", "d:00001"]
    assert cited_ids("plain") == []

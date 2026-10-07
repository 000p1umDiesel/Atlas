import uuid

from mnogobase import ids


def test_normalize_name():
    assert ids.normalize_name("  Self-Attention!! ") == "self attention"
    assert ids.normalize_name("Механизм  внимания") == "механизм внимания"
    assert ids.normalize_name("C++") == "c++"
    assert ids.normalize_name("C#") == "c#"
    assert ids.normalize_name("ＴＲＡＮＳＦＯＲＭＥＲ") == "transformer"  # NFKC full-width


def test_entity_id_is_stable_case_insensitive_and_type_sensitive():
    a = ids.entity_id("Method", "Self-Attention")
    assert a == ids.entity_id("method", "self attention")
    assert a != ids.entity_id("Concept", "Self-Attention")
    assert len(a) == 16


def test_chunk_id_and_point_id():
    assert ids.chunk_id("abcdef0123456789", 7) == "abcdef0123456789:00007"
    p = ids.point_id("abcdef0123456789:00007")
    assert uuid.UUID(p)
    assert p == ids.point_id("abcdef0123456789:00007")
    assert p != ids.point_id("abcdef0123456789:00008")


def test_slugify():
    assert ids.slugify("Retrieval-Augmented Generation") == "retrieval-augmented-generation"
    assert ids.slugify("!!!") == "untitled"


def test_file_doc_id_depends_on_content_only(tmp_path):
    a = tmp_path / "a.md"
    b = tmp_path / "sub" / "b.md"
    b.parent.mkdir()
    a.write_text("same", encoding="utf-8")
    b.write_text("same", encoding="utf-8")
    assert ids.file_doc_id(a) == ids.file_doc_id(b)
    b.write_text("different", encoding="utf-8")
    assert ids.file_doc_id(a) != ids.file_doc_id(b)


def test_slugify_never_contains_hash():
    assert "#" not in ids.slugify("C#")
    assert ids.slugify("C#") == "csharp"
    assert ids.slugify("C++") == "c++"
    assert "#" not in ids.slugify("a # b")


def test_slugify_caps_length_with_stable_hash():
    long_name = "Механизм " * 30  # ~270 chars of Cyrillic
    slug = ids.slugify(long_name)
    assert len(slug) <= 80
    assert len((slug + ".md").encode("utf-8")) <= 255
    assert slug == ids.slugify(long_name)
    other = ids.slugify(long_name + " внимания")
    assert len(other) <= 80
    assert slug != other


def test_slugify_caps_bytes_for_wide_scripts():
    slug = ids.slugify("𠀀" * 100)  # 4-byte UTF-8 word characters
    assert len((slug + ".md").encode("utf-8")) <= 255


# Golden values: stored Qdrant points, graph ids and wiki pages are keyed by these hashes.
# A change here silently orphans every existing index, so it must be deliberate.
def test_namespace_is_pinned():
    assert str(ids.NAMESPACE) == "5b0c9a8e-3f61-4d2a-9b7e-0c1d2e3f4a5b"


def test_point_id_golden_values():
    assert ids.point_id("abc:00001") == "3abf48ae-e1b7-5cab-8afa-229e4a8c8f55"
    assert ids.point_id("chunk:0123456789abcdef:00000") == "6205ee60-de3b-5dea-bf3e-373d09f1a7ee"


def test_entity_id_golden_values():
    assert ids.entity_id("Method", "Attention Mechanism") == "42454f7230bee8f7"
    assert ids.entity_id("Person", "Ashish Vaswani") == "5f2cfae423c91f73"


def test_file_doc_id_golden_value(tmp_path):
    path = tmp_path / "golden.md"
    path.write_bytes(b"mnogobase golden\n")
    assert ids.file_doc_id(path) == "2be6f3db35862591"

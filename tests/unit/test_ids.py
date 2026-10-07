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

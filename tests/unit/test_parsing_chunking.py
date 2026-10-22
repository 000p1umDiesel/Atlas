import json
from pathlib import Path

import httpx

from mnogobase.chunking import hybrid
from mnogobase.chunking.hybrid import Chunker
from mnogobase.chunking.tokenizer import ServerTokenizer
from mnogobase.config import ChunkingSettings, EmbedderSettings, ParsingSettings
from mnogobase.parsing.docling_parser import DoclingParser

FIXTURES = Path(__file__).parents[1] / "fixtures"
DOC = "d" * 16


def test_parse_markdown_cache_roundtrip(tmp_path):
    parser = DoclingParser(ParsingSettings(), "cpu", tmp_path)
    parsed = parser.parse(FIXTURES / "attention_en.md", DOC)
    assert parsed.title == "Attention Is All You Need"
    assert parsed.mime == "text/markdown"
    assert parser.cache_path(DOC).exists()
    again = parser.load(DOC, FIXTURES / "attention_en.md")
    assert again.title == parsed.title
    assert again.doc.export_to_markdown() == parsed.doc.export_to_markdown()
    parser.drop_cache(DOC)
    assert not parser.cache_path(DOC).exists()


def test_title_falls_back_to_file_stem(tmp_path):
    src = tmp_path / "notes.md"
    src.write_text("Just a paragraph without headings.\n", encoding="utf-8")
    parsed = DoclingParser(ParsingSettings(), "cpu", tmp_path / "cache").parse(src, DOC)
    assert parsed.title == "notes"


def test_chunker_records(tmp_path):
    parser = DoclingParser(ParsingSettings(), "cpu", tmp_path)
    parsed = parser.parse(FIXTURES / "vnimanie_ru.md", DOC)
    settings = ChunkingSettings(tokenizer="Qwen/Qwen3-Embedding-4B", max_tokens=64)
    chunks = Chunker(settings, EmbedderSettings()).chunk(parsed, DOC, "/x/vnimanie_ru.md")
    assert len(chunks) >= 2
    assert [c.idx for c in chunks] == list(range(len(chunks)))
    assert chunks[0].chunk_id == f"{DOC}:00000"
    assert all(c.text and c.n_tokens > 0 for c in chunks)
    assert max(c.n_tokens for c in chunks) <= 96
    assert any("Как считаются веса" in c.headings for c in chunks)
    first = chunks[0]
    assert first.headings and first.context_text.startswith(first.headings[0])
    assert all(c.path == "/x/vnimanie_ru.md" and c.doc_id == DOC for c in chunks)
    assert all(c.page_start is None for c in chunks)  # у markdown нет страниц


def test_chunker_splits_long_text_with_a_server_tokenizer(tmp_path, monkeypatch):
    # llama.cpp /tokenize: токен — слово; при 16 токенах длинные абзацы режет semchunk
    def tokenize(request):
        text = json.loads(request.content)["content"]
        return httpx.Response(200, json={"tokens": list(range(len(text.split())))})

    client = httpx.Client(base_url="http://llama:8001", transport=httpx.MockTransport(tokenize))
    tokenizer = ServerTokenizer(client=client, max_tokens=16)
    monkeypatch.setattr(hybrid, "load_tokenizer", lambda settings, embedder: tokenizer)
    parsed = DoclingParser(ParsingSettings(), "cpu", tmp_path).parse(
        FIXTURES / "attention_en.md", DOC
    )
    chunks = Chunker(ChunkingSettings(max_tokens=16), EmbedderSettings()).chunk(parsed, DOC, "/x")
    assert len(chunks) > 5
    assert all(0 < c.n_tokens <= 16 for c in chunks)

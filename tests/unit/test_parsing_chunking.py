import json
from pathlib import Path

import httpx
import pytest
from docling.datamodel.base_models import ApiImageRequestResult, InputFormat, VlmStopReason
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling_core.transforms.chunker.hierarchical_chunker import ChunkingDocSerializer
from docling_core.types.doc import DescriptionMetaField, DoclingDocument, PictureItem, PictureMeta
from docling_core.types.doc.labels import PictureClassificationLabel
from PIL import Image

from mnogobase.chunking import hybrid
from mnogobase.chunking.hybrid import Chunker
from mnogobase.chunking.tokenizer import ServerTokenizer
from mnogobase.config import ChunkingSettings, EmbedderSettings, ParsingSettings, VisionSettings
from mnogobase.parsing import docling_parser
from mnogobase.parsing.docling_parser import DoclingParser, VisionError

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


VISION = VisionSettings(enabled=True, base_url="http://vl:8003/v1", model="vl", skip=["logo"])


def test_vision_sends_pictures_to_the_vlm_server(tmp_path):
    parser = DoclingParser(ParsingSettings(ocr_lang=["iso:ru"]), "cpu", tmp_path, VISION)
    formats = parser._get_converter().format_to_options
    pdf = formats[InputFormat.PDF].pipeline_options
    assert isinstance(pdf, PdfPipelineOptions) and pdf.ocr_options.lang == ["iso:ru-Cyrl"]
    assert formats[InputFormat.IMAGE].pipeline_options is pdf
    for fmt in (InputFormat.PDF, InputFormat.DOCX, InputFormat.PPTX):
        options = formats[fmt].pipeline_options
        assert options.do_picture_description and options.enable_remote_services
        assert options.do_picture_classification  # для фильтра skip
        api = options.picture_description_options
        assert str(api.url) == "http://vl:8003/v1/chat/completions"
        assert api.params == {"model": "vl", "max_tokens": 400, **VISION.params}
        assert api.classification_deny == [PictureClassificationLabel.LOGO]
        assert api.classification_min_confidence == 0.5  # иначе отсеивается всё


def test_without_vision_pictures_are_not_described(tmp_path):
    parser = DoclingParser(ParsingSettings(), "cpu", tmp_path, VisionSettings())
    formats = parser._get_converter().format_to_options
    assert not formats[InputFormat.PDF].pipeline_options.do_picture_description
    assert not formats[InputFormat.DOCX].pipeline_options.do_picture_description


def test_an_image_file_is_described_whole_before_its_ocr_text(tmp_path, monkeypatch):
    shot = tmp_path / "shot.png"
    Image.new("RGB", (40, 20), "white").save(shot)
    calls: list = []

    def fake_request(image, prompt, url, **kwargs):
        calls.append((image.size, str(url), kwargs["model"]))
        return ApiImageRequestResult(
            text="Панель склада: подшипник 6205, 1 240 шт.",
            num_tokens=12,
            stop_reason=VlmStopReason.END_OF_SEQUENCE,
        )

    monkeypatch.setattr(docling_parser, "api_image_request", fake_request)
    doc = DoclingDocument(name="shot")
    doc.add_text(label="text", text="OCR text")
    docling_parser._describe_whole_image(doc, shot, VISION)
    assert calls == [((40, 20), "http://vl:8003/v1/chat/completions", "vl")]
    first = doc.body.children[0].resolve(doc)
    assert isinstance(first, PictureItem) and first.prov[0].page_no == 1
    # описание попадает в текст чанков, перед текстом OCR
    text = ChunkingDocSerializer(doc=doc).serialize().text
    assert text.index("подшипник 6205") < text.index("OCR text")


def test_a_failed_description_fails_the_parse(tmp_path, monkeypatch):
    # Docling глушит ошибки API: описание остаётся пустым
    doc = DoclingDocument(name="d")
    picture = doc.add_picture()
    picture.meta = PictureMeta(description=DescriptionMetaField(text="", created_by="vl"))
    with pytest.raises(VisionError, match="1 of 1 pictures got no description"):
        docling_parser._check_descriptions(doc, VISION)

    shot = tmp_path / "shot.png"
    Image.new("RGB", (4, 4)).save(shot)
    failed = ApiImageRequestResult(
        text="", num_tokens=0, stop_reason=VlmStopReason.INFERENCE_ERROR, error="ConnectError"
    )
    monkeypatch.setattr(docling_parser, "api_image_request", lambda *a, **k: failed)
    with pytest.raises(VisionError, match="shot.png: the VLM at http://vl:8003/v1 failed"):
        docling_parser._describe_whole_image(DoclingDocument(name="s"), shot, VISION)

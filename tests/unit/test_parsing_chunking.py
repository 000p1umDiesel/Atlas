import base64
import io
import json
from pathlib import Path

import httpx
import pytest
from docling.backend.image_backend import ImageDocumentBackend
from docling.datamodel.base_models import ConversionStatus, InputFormat
from docling.datamodel.document import ConversionResult, InputDocument
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling_core.transforms.chunker.hierarchical_chunker import ChunkingDocSerializer
from docling_core.types.doc import (
    BoundingBox,
    CoordOrigin,
    DescriptionMetaField,
    DoclingDocument,
    DocumentOrigin,
    ImageRef,
    PictureItem,
    PictureMeta,
    ProvenanceItem,
    Size,
)
from docling_core.types.doc.labels import PictureClassificationLabel
from docx import Document
from PIL import Image
from tenacity import wait_none
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace
from transformers import PreTrainedTokenizerFast

from mnogobase.chunking import hybrid
from mnogobase.chunking.hybrid import Chunker
from mnogobase.chunking.tokenizer import ServerTokenizer
from mnogobase.core.config import (
    ChunkingSettings,
    EmbedderSettings,
    ParsingSettings,
    VisionSettings,
)
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


def test_parse_docx_saves_embedded_image_and_loads_its_pixels_from_cache(tmp_path):
    image_path = tmp_path / "stock.png"
    image = Image.new("RGB", (12, 8), (12, 90, 160))
    image.putpixel((0, 0), (200, 5, 80))
    image.save(image_path)
    source = tmp_path / "stock.docx"
    office = Document()
    office.add_heading("Warehouse stock", 0)
    office.add_paragraph("The stock diagram is embedded below.")
    office.add_picture(str(image_path))
    office.save(source)

    parser = DoclingParser(ParsingSettings(ocr=False), "cpu", tmp_path / "cache")
    parsed = parser.parse(source, DOC)
    assert parsed.title == "Warehouse stock"
    assert parsed.mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert parsed.n_pages is None  # DOCX does not carry page provenance in Docling
    images_dir = tmp_path / "cache" / DOC / "images"
    assert json.loads((images_dir / "images.json").read_text(encoding="utf-8")) == [
        {"file": "img_0000.png", "caption": "", "page": None}
    ]
    with Image.open(images_dir / "img_0000.png") as saved:
        assert saved.size == (12, 8)
        assert saved.getpixel((0, 0)) == (200, 5, 80)
        assert saved.getpixel((11, 7)) == (12, 90, 160)

    image_path.unlink()
    source.unlink()
    loaded = parser.load(DOC, source)
    assert loaded.title == "Warehouse stock"
    assert "The stock diagram is embedded below." in loaded.doc.export_to_markdown()
    [picture] = loaded.doc.pictures
    restored = picture.get_image(loaded.doc)
    assert restored is not None and restored.size == (12, 8)
    assert restored.getpixel((0, 0)) == (200, 5, 80)
    assert restored.getpixel((11, 7)) == (12, 90, 160)

    parser.drop_cache(DOC)
    assert not parser.cache_path(DOC).exists()
    assert not images_dir.parent.exists()


def test_save_images_skips_unavailable_pixels_and_keeps_captions_and_pages(tmp_path):
    doc = DoclingDocument(name="stock")
    doc.add_text(label="text", text="Stock diagrams")
    doc.add_picture()  # no image bytes and no page image to crop
    caption = doc.add_text(label="caption", text="Рисунок 1: остаток склада")
    doc.add_picture(
        image=ImageRef.from_pil(Image.new("RGB", (4, 3), (20, 40, 60)), dpi=72),
        caption=caption,
        prov=ProvenanceItem(
            page_no=3,
            bbox=BoundingBox(l=0, t=0, r=4, b=3, coord_origin=CoordOrigin.TOPLEFT),
            charspan=(0, 0),
        ),
    )
    doc.add_picture()  # skipped pictures must not leave gaps in saved file names
    doc.add_picture(image=ImageRef.from_pil(Image.new("RGB", (2, 5), "yellow"), dpi=72))

    images_dir = tmp_path / "images"
    assert docling_parser._save_images(doc, images_dir) == 2
    assert json.loads((images_dir / "images.json").read_text(encoding="utf-8")) == [
        {"file": "img_0000.png", "caption": "Рисунок 1: остаток склада", "page": 3},
        {"file": "img_0001.png", "caption": "", "page": None},
    ]
    assert {file.name for file in images_dir.iterdir()} == {
        "images.json",
        "img_0000.png",
        "img_0001.png",
    }
    for name, size, color in (
        ("img_0000.png", (4, 3), (20, 40, 60)),
        ("img_0001.png", (2, 5), (255, 255, 0)),
    ):
        with Image.open(images_dir / name) as saved:
            assert saved.size == size
            assert saved.tobytes() == bytes(color) * (size[0] * size[1])


def test_unavailable_picture_pixels_do_not_create_an_empty_image_manifest(tmp_path):
    doc = DoclingDocument(name="stock")
    doc.add_picture()
    images_dir = tmp_path / "images"
    assert docling_parser._save_images(doc, images_dir) == 0
    assert not images_dir.exists()


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


def test_server_tokenizer_retries_server_errors_and_closes_its_client():
    replies = [httpx.Response(503), httpx.Response(200, json={"tokens": [1, 2, 3]})]
    client = httpx.Client(
        base_url="http://llama:8001", transport=httpx.MockTransport(lambda r: replies.pop(0))
    )
    tokenizer = ServerTokenizer(client=client, max_tokens=16, retry_wait=wait_none())
    assert tokenizer.count_tokens("one two three") == 3 and replies == []
    chunker = Chunker(ChunkingSettings(), EmbedderSettings())
    chunker._tokenizer = tokenizer
    chunker.close()
    assert client.is_closed


VISION = VisionSettings(enabled=True, base_url="http://vl:8003/v1", model="vl", skip=["logo"])


@pytest.fixture
def image_parser(tmp_path, monkeypatch):
    """Keep real image/document data while avoiding the heavyweight layout models."""
    source = tmp_path / "shot.png"
    image = Image.new("RGBA", (16, 8), (12, 90, 160, 255))
    image.putpixel((0, 0), (200, 5, 80, 255))
    image.save(source)
    doc = DoclingDocument(
        name="shot",
        origin=DocumentOrigin(filename="shot.png", mimetype="image/png", binary_hash=0),
    )
    doc.add_page(
        page_no=1,
        size=Size(width=80, height=40),
        image=ImageRef.from_pil(image.convert("RGB"), dpi=72),
    )
    doc.add_text(
        label="text",
        text="OCR text: stock count 1240.",
        prov=ProvenanceItem(
            page_no=1,
            bbox=BoundingBox(l=0, t=0, r=80, b=40, coord_origin=CoordOrigin.TOPLEFT),
            charspan=(0, 27),
        ),
    )
    input_doc = InputDocument(source, InputFormat.IMAGE, ImageDocumentBackend)
    result = ConversionResult(input=input_doc, document=doc, status=ConversionStatus.SUCCESS)
    parser = DoclingParser(ParsingSettings(ocr=False), "cpu", tmp_path / "cache", VISION)

    def convert(path, *, raises_on_error, max_num_pages):
        assert path == source and max_num_pages == 1000
        return result

    monkeypatch.setattr(parser._get_converter(), "convert", convert)
    try:
        yield parser, source
    finally:
        input_doc._backend.unload()


def test_parse_image_caches_whole_image_description_before_chunking(
    image_parser, tmp_path, monkeypatch
):
    parser, source = image_parser
    requests = []
    clients = []
    real_client = httpx.Client

    def respond(request):
        assert str(request.url) == "http://vl:8003/v1/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == "vl" and body["max_tokens"] == 400
        encoded_image, prompt = body["messages"][0]["content"]
        assert prompt == {"type": "text", "text": VISION.prompt}
        prefix, encoded = encoded_image["image_url"]["url"].split(",", 1)
        assert prefix == "data:image/png;base64"
        with Image.open(io.BytesIO(base64.b64decode(encoded))) as sent:
            assert sent.mode == "RGB" and sent.size == (16, 8)
            assert sent.getpixel((0, 0)) == (200, 5, 80)
        requests.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Bearing 6205: 1240 units in stock."}}]},
        )

    def client(**kwargs):
        http = real_client(**kwargs, transport=httpx.MockTransport(respond))
        clients.append(http)
        return http

    monkeypatch.setattr(docling_parser.httpx, "Client", client)
    parsed = parser.parse(source, DOC)
    assert parsed.mime == "image/png" and parsed.n_pages == 1
    assert len(requests) == 1 and all(http.is_closed for http in clients)
    first = parsed.doc.body.children[0].resolve(parsed.doc)
    assert isinstance(first, PictureItem)
    assert first.prov[0].page_no == 1
    assert first.prov[0].bbox.as_tuple() == (0, 0, 80, 40)
    images_dir = tmp_path / "cache" / DOC / "images"
    assert json.loads((images_dir / "images.json").read_text(encoding="utf-8")) == [
        {"file": "img_0000.png", "caption": "", "page": 1}
    ]
    with Image.open(images_dir / "img_0000.png") as saved:
        assert saved.size == (16, 8)
        assert saved.getpixel((0, 0)) == (200, 5, 80)

    source.unlink()
    loaded = parser.load(DOC, source)
    serialized = ChunkingDocSerializer(doc=loaded.doc).serialize().text
    assert serialized.index("Bearing 6205: 1240 units in stock.") < serialized.index("OCR text")
    tokenizer = Tokenizer(WordLevel({"[UNK]": 0}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = Whitespace()
    tokenizer_dir = tmp_path / "tokenizer"
    PreTrainedTokenizerFast(tokenizer_object=tokenizer, unk_token="[UNK]").save_pretrained(
        tokenizer_dir
    )
    chunks = Chunker(
        ChunkingSettings(tokenizer=str(tokenizer_dir), max_tokens=64), EmbedderSettings()
    ).chunk(loaded, DOC, str(source))
    assert chunks and chunks[0].text.startswith("Bearing 6205: 1240 units in stock.")
    assert any("OCR text: stock count 1240." in chunk.text for chunk in chunks)
    assert all(chunk.page_start == chunk.page_end == 1 for chunk in chunks)


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (400, {"error": "invalid image"}, "HTTPStatusError"),
        (200, {"choices": [{"message": {"content": ""}}]}, "пустое описание"),
        (200, {"choices": []}, "IndexError"),
    ],
)
def test_parse_image_does_not_cache_failed_descriptions(
    image_parser, monkeypatch, status, body, error
):
    parser, source = image_parser
    http = _vlm(lambda request: httpx.Response(status, json=body))
    monkeypatch.setattr(docling_parser.httpx, "Client", lambda **kwargs: http)
    with pytest.raises(VisionError, match=error):
        parser.parse(source, DOC)
    assert http.is_closed
    assert not parser.cache_path(DOC).exists()
    assert not (parser.cache_path(DOC).parent / DOC).exists()


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


def _vlm(respond):
    return httpx.Client(base_url="http://vl:8003/v1", transport=httpx.MockTransport(respond))


def test_an_image_file_is_described_whole_before_its_ocr_text(tmp_path):
    shot = tmp_path / "shot.png"
    Image.new("RGB", (40, 20), "white").save(shot)
    requests: list = []

    def respond(request):
        requests.append((str(request.url), json.loads(request.content)))
        message = {"content": "Панель склада: подшипник 6205, 1 240 шт."}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    doc = DoclingDocument(name="shot")
    doc.add_text(label="text", text="OCR text")
    docling_parser._describe_whole_image(doc, shot, VISION, _vlm(respond))
    [(url, body)] = requests
    assert url == "http://vl:8003/v1/chat/completions"
    assert (body["model"], body["max_tokens"], body["temperature"]) == ("vl", 400, 0.0)
    image, prompt = body["messages"][0]["content"]
    assert image["image_url"]["url"].startswith("data:image/png;base64,")
    assert prompt == {"type": "text", "text": VISION.prompt}
    first = doc.body.children[0].resolve(doc)
    assert isinstance(first, PictureItem) and first.prov[0].page_no == 1
    # описание попадает в текст чанков, перед текстом OCR
    text = ChunkingDocSerializer(doc=doc).serialize().text
    assert text.index("подшипник 6205") < text.index("OCR text")


def test_a_large_image_is_shrunk_before_the_vlm(tmp_path):
    shot = tmp_path / "huge.png"
    Image.new("RGB", (4096, 1024), "white").save(shot)
    sizes: list = []

    def respond(request):
        url = json.loads(request.content)["messages"][0]["content"][0]["image_url"]["url"]
        with Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as sent:
            sizes.append(sent.size)
        return httpx.Response(200, json={"choices": [{"message": {"content": "wide chart"}}]})

    docling_parser._describe_whole_image(DoclingDocument(name="huge"), shot, VISION, _vlm(respond))
    assert sizes == [(2048, 512)]


def test_files_over_the_parsing_limits_fail_before_docling(tmp_path):
    big = tmp_path / "big.md"
    big.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(docling_parser.ParseLimitError, match="max_file_mb"):
        DoclingParser(ParsingSettings(max_file_mb=1), "cpu", tmp_path / "c").parse(big, DOC)
    pdf = tmp_path / "pages.pdf"
    pages = [Image.new("RGB", (100, 100), "white") for _ in range(3)]
    pages[0].save(pdf, save_all=True, append_images=pages[1:])
    parser = DoclingParser(ParsingSettings(max_pages=2, ocr=False), "cpu", tmp_path / "c")
    with pytest.raises(docling_parser.ParseLimitError, match="3 стр."):
        parser.parse(pdf, DOC)
    assert not parser.cache_path(DOC).exists()


def test_a_failed_description_fails_the_parse(tmp_path):
    # Docling глушит ошибки API: описание остаётся пустым
    doc = DoclingDocument(name="d")
    picture = doc.add_picture()
    picture.meta = PictureMeta(description=DescriptionMetaField(text="", created_by="vl"))
    with pytest.raises(VisionError, match="1 из 1 изображений не получили описания"):
        docling_parser._check_descriptions(doc, VISION)

    shot = tmp_path / "shot.png"
    Image.new("RGB", (4, 4)).save(shot)
    attempts: list = []

    def down(request):
        attempts.append(request)
        raise httpx.ConnectError("refused")

    with pytest.raises(VisionError, match="shot.png: ошибка VLM по адресу http://vl:8003/v1: Conn"):
        docling_parser._describe_whole_image(
            DoclingDocument(name="s"), shot, VISION, _vlm(down), retry_wait=lambda state: 0
        )
    assert len(attempts) == 3  # сетевые ошибки повторяются
    empty = _vlm(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": ""}}]}))
    with pytest.raises(VisionError, match="вернула пустое описание"):
        docling_parser._describe_whole_image(DoclingDocument(name="s"), shot, VISION, empty)

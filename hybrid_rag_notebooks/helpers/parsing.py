"""Разбор документов через Docling и адаптеры токенизатора для RAG."""

from __future__ import annotations

import base64
import hashlib
import io
from pathlib import Path

from docling_core.transforms.chunker.tokenizer.base import BaseTokenizer
from pydantic import ConfigDict, PrivateAttr

EXTENSIONS = {
    ".pdf",
    ".docx",
    ".pptx",
    ".xlsx",
    ".html",
    ".htm",
    ".md",
    ".adoc",
    ".csv",
    ".txt",
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".bmp",
    ".webp",
}
IMAGES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


class ServerTokenizer(BaseTokenizer):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    api: object
    max_tokens: int = 512
    _cache: dict[str, int] = PrivateAttr(default_factory=dict)

    def count_tokens(self, text: str) -> int:
        if text not in self._cache:
            if len(self._cache) >= 50000:
                self._cache.clear()
            root = str(self.api.http.base_url).rstrip("/").removesuffix("/v1")
            self._cache[text] = len(
                self.api.request(
                    "POST", root + "/tokenize", json={"content": text, "add_special": False}
                )["tokens"]
            )
        return self._cache[text]

    def get_max_tokens(self) -> int:
        return self.max_tokens

    def get_tokenizer(self):
        return self.count_tokens


def make_tokenizer(api, max_tokens: int = 512, hf_repo: str = ""):
    if hf_repo:
        from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
        from transformers import AutoTokenizer

        return HuggingFaceTokenizer(
            tokenizer=AutoTokenizer.from_pretrained(hf_repo), max_tokens=max_tokens
        )
    tokenizer = ServerTokenizer(api=api, max_tokens=max_tokens)
    tokenizer.count_tokens("tokenizer probe")
    return tokenizer


def discover_documents(root: Path) -> list[Path]:
    paths = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in EXTENSIONS)
    if not paths:
        raise ValueError(f"{root}: no supported documents; expected {sorted(EXTENSIONS)}")
    return paths


def describe_image(image, vision) -> str:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG")
    url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    response = vision.chat(
        [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": url}},
                    {
                        "type": "text",
                        "text": "Describe the image in detail for document retrieval. "
                        "Preserve visible facts, labels, numbers and relationships. Do not invent facts.",
                    },
                ],
            }
        ],
        max_tokens=400,
    )
    text = response["choices"][0]["message"]["content"] or ""
    if not text.strip():
        raise ValueError("VLM returned an empty picture description")
    return text.strip()


class DocumentParser:
    def __init__(
        self,
        *,
        ocr=True,
        device="cpu",
        vision=None,
        min_area=0.05,
        skip=("logo", "icon", "qr_code", "bar_code", "signature", "stamp"),
    ):
        from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import (
            ConvertPipelineOptions,
            OcrAutoOptions,
            PdfPipelineOptions,
        )
        from docling.document_converter import (
            DocumentConverter,
            ImageFormatOption,
            PdfFormatOption,
            PowerpointFormatOption,
            WordFormatOption,
        )

        self.vision, self.min_area, self.skip = vision, min_area, set(skip)
        options = PdfPipelineOptions(
            do_ocr=ocr,
            ocr_options=OcrAutoOptions(lang=["iso:ru", "iso:en"]),
            generate_picture_images=True,
            images_scale=2,
            accelerator_options=AcceleratorOptions(device=AcceleratorDevice(device)),
        )
        options.do_picture_classification = vision is not None and bool(skip)
        formats = {
            InputFormat.PDF: PdfFormatOption(pipeline_options=options),
            InputFormat.IMAGE: ImageFormatOption(pipeline_options=options),
        }
        if vision is not None:
            office = ConvertPipelineOptions()
            office.do_picture_classification = bool(skip)
            formats.update(
                {
                    InputFormat.DOCX: WordFormatOption(pipeline_options=office),
                    InputFormat.PPTX: PowerpointFormatOption(pipeline_options=office),
                }
            )
        self.converter = DocumentConverter(format_options=formats)

    def parse(self, path: Path):
        from docling_core.types.doc import DocItemLabel, DoclingDocument

        if path.suffix.lower() == ".txt":
            # У Docling нет встроенного обработчика TXT: сохраняем текст по абзацам.
            doc = DoclingDocument(name=path.stem)
            for paragraph in path.read_text(encoding="utf-8-sig").split("\n\n"):
                if paragraph.strip():
                    doc.add_text(label=DocItemLabel.TEXT, text=paragraph.strip())
            return doc
        result = self.converter.convert(path, raises_on_error=True)
        if str(result.status.value) not in ("success",):
            raise ValueError(f"{path}: Docling status {result.status}; {result.errors}")
        doc = result.document
        if self.vision is None:
            return doc
        if path.suffix.lower() in IMAGES:
            from PIL import Image

            with Image.open(path) as image:
                text = describe_image(image, self.vision)
            doc.add_text(label=DocItemLabel.TEXT, text="Image description: " + text)
        else:
            for picture in list(doc.pictures):
                predictions = getattr(
                    getattr(picture.meta, "classification", None), "predictions", []
                )
                if any(
                    str(p.class_name) in self.skip and (p.confidence or 0) >= 0.5
                    for p in predictions
                ):
                    continue
                if picture.prov:
                    prov = picture.prov[0]
                    page = doc.pages.get(prov.page_no)
                    if (
                        page
                        and prov.bbox.area() / (page.size.width * page.size.height) < self.min_area
                    ):
                        continue
                image = picture.get_image(doc)
                if image is None:
                    raise ValueError(f"{path}: no pixels available for picture {picture.self_ref}")
                text = describe_image(image, self.vision)
                # Добавляем описание отдельным текстовым узлом, чтобы HybridChunker включил его в индекс.
                doc.add_text(
                    label=DocItemLabel.TEXT,
                    text="Image description: " + text,
                    prov=picture.prov[0] if picture.prov else None,
                )
        return doc


def chunk_document(doc, *, path: str, doc_id: str, tokenizer) -> list[dict]:
    from docling_core.transforms.chunker.hybrid_chunker import HybridChunker

    chunker = HybridChunker(tokenizer=tokenizer, merge_peers=True)
    chunks = []
    for raw in chunker.chunk(doc):
        if not raw.text.strip():
            continue
        pages = sorted({p.page_no for item in raw.meta.doc_items for p in item.prov})
        idx = len(chunks)
        chunks.append(
            {
                "chunk_id": f"{doc_id}:{idx:05d}",
                "doc_id": doc_id,
                "idx": idx,
                "text": raw.text.strip(),
                "headings": list(raw.meta.headings or []),
                "page": pages[0] if pages else None,
                "path": path,
                "n_tokens": tokenizer.count_tokens(chunker.contextualize(raw)),
            }
        )
    if not chunks:
        raise ValueError(f"{path}: parsing produced no text chunks")
    return chunks


def document_inventory(paths: list[Path], root: Path) -> list[dict]:
    return [
        {
            "path": p.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        }
        for p in paths
    ]

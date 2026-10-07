from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.doc import DocItemLabel, DoclingDocument, PictureItem

from mnogobase.config import ParsingSettings
from mnogobase.log import get_logger

_CUDA_BATCH_SIZE = 16
_BATCH_SIZE_ATTRS = ("layout_batch_size", "ocr_batch_size", "table_batch_size")


@dataclass
class ParsedDocument:
    doc: DoclingDocument
    title: str
    mime: str
    n_pages: int | None


class DoclingParser:
    def __init__(self, settings: ParsingSettings, device: str, cache_dir: Path):
        self._s = settings
        self._device = device
        self._cache = cache_dir
        self._converter: DocumentConverter | None = None
        self._log = get_logger(__name__)

    def _get_converter(self) -> DocumentConverter:
        if self._converter is None:
            pdf_options = PdfPipelineOptions(
                do_ocr=self._s.ocr,
                generate_picture_images=True,  # kept for future multimodal embedding
                images_scale=2.0,
                accelerator_options=AcceleratorOptions(device=AcceleratorDevice(self._device)),
            )
            if self._device == "cuda":
                for attr in _BATCH_SIZE_ATTRS:
                    if hasattr(pdf_options, attr):
                        setattr(pdf_options, attr, _CUDA_BATCH_SIZE)
            self._converter = DocumentConverter(
                format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options)}
            )
        return self._converter

    def cache_path(self, doc_id: str) -> Path:
        return self._cache / f"{doc_id}.json"

    def parse(self, path: Path, doc_id: str) -> ParsedDocument:
        result = self._get_converter().convert(path, raises_on_error=True)
        doc = result.document
        self._cache.mkdir(parents=True, exist_ok=True)
        self.cache_path(doc_id).write_text(doc.model_dump_json(), encoding="utf-8")
        n_images = _save_images(doc, self._cache / doc_id / "images")
        parsed = _wrap(doc, path)
        self._log.info(
            "parsed", doc_id=doc_id, title=parsed.title, n_pages=parsed.n_pages, n_images=n_images
        )
        return parsed

    def load(self, doc_id: str, path: Path) -> ParsedDocument:
        doc = DoclingDocument.model_validate_json(
            self.cache_path(doc_id).read_text(encoding="utf-8")
        )
        return _wrap(doc, path)

    def drop_cache(self, doc_id: str) -> None:
        self.cache_path(doc_id).unlink(missing_ok=True)
        shutil.rmtree(self._cache / doc_id, ignore_errors=True)


def _wrap(doc: DoclingDocument, path: Path) -> ParsedDocument:
    mime = doc.origin.mimetype if doc.origin else "application/octet-stream"
    return ParsedDocument(
        doc=doc, title=_title(doc, path), mime=mime, n_pages=len(doc.pages) or None
    )


def _title(doc: DoclingDocument, path: Path) -> str:
    for item, _level in doc.iterate_items():
        if getattr(item, "label", None) == DocItemLabel.TITLE and item.text.strip():
            return item.text.strip()
    return path.stem


def _save_images(doc: DoclingDocument, out_dir: Path) -> int:
    manifest = []
    for item, _level in doc.iterate_items():
        if not isinstance(item, PictureItem):
            continue
        image = item.get_image(doc)
        if image is None:
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        name = f"img_{len(manifest):04d}.png"
        image.save(out_dir / name)
        manifest.append(
            {
                "file": name,
                "caption": item.caption_text(doc),
                "page": item.prov[0].page_no if item.prov else None,
            }
        )
    if manifest:
        (out_dir / "images.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return len(manifest)

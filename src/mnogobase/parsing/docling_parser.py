from __future__ import annotations

import base64
import io
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import httpx
from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import (
    ConvertPipelineOptions,
    OcrAutoOptions,
    PdfPipelineOptions,
    PictureDescriptionApiOptions,
)
from docling.document_converter import (
    DocumentConverter,
    FormatOption,
    ImageFormatOption,
    PdfFormatOption,
    PowerpointFormatOption,
    WordFormatOption,
)
from docling_core.types.doc import (
    BoundingBox,
    CoordOrigin,
    DescriptionMetaField,
    DocItemLabel,
    DoclingDocument,
    PictureItem,
    PictureMeta,
    ProvenanceItem,
)
from docling_core.types.doc.labels import PictureClassificationLabel
from PIL import Image

from mnogobase.config import ParsingSettings, VisionSettings
from mnogobase.http import DEFAULT_RETRY_WAIT, auth_headers, post_json_with_retry
from mnogobase.log import get_logger

_CUDA_BATCH_SIZE = 16
_BATCH_SIZE_ATTRS = ("layout_batch_size", "ocr_batch_size", "table_batch_size")
_SKIP_CONFIDENCE = 0.5  # картинка не описывается, если класс из vision.skip вероятнее этого


class VisionError(RuntimeError):
    """VLM не описал картинку: документ не должен считаться разобранным без описаний."""


@dataclass
class ParsedDocument:
    doc: DoclingDocument
    title: str
    mime: str
    n_pages: int | None


class DoclingParser:
    def __init__(
        self,
        settings: ParsingSettings,
        device: str,
        cache_dir: Path,
        vision: VisionSettings | None = None,
    ):
        self._s = settings
        self._vision = vision
        self._device = device
        self._cache = cache_dir
        self._converter: DocumentConverter | None = None
        self._log = get_logger(__name__)

    def _get_converter(self) -> DocumentConverter:
        if self._converter is None:
            pdf_options = PdfPipelineOptions(
                do_ocr=self._s.ocr,
                ocr_options=OcrAutoOptions(lang=self._s.ocr_lang),
                generate_picture_images=True,  # картинки сохраняются в кэш разбора (images/)
                images_scale=2.0,
                accelerator_options=AcceleratorOptions(device=AcceleratorDevice(self._device)),
            )
            if self._device == "cuda":
                for attr in _BATCH_SIZE_ATTRS:
                    if hasattr(pdf_options, attr):
                        setattr(pdf_options, attr, _CUDA_BATCH_SIZE)
            # картинки (скриншоты) идут тем же конвейером, что и PDF: разметка, OCR, описание
            formats: dict[InputFormat, FormatOption] = {
                InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options),
                InputFormat.IMAGE: ImageFormatOption(pipeline_options=pdf_options),
            }
            if self._vision is not None and self._vision.enabled:
                _describe_pictures(pdf_options, self._vision)
                office = ConvertPipelineOptions()
                _describe_pictures(office, self._vision)
                formats[InputFormat.DOCX] = WordFormatOption(pipeline_options=office)
                formats[InputFormat.PPTX] = PowerpointFormatOption(pipeline_options=office)
            self._converter = DocumentConverter(format_options=formats)
        return self._converter

    def cache_path(self, doc_id: str) -> Path:
        return self._cache / f"{doc_id}.json"

    def parse(self, path: Path, doc_id: str) -> ParsedDocument:
        result = self._get_converter().convert(path, raises_on_error=True)
        doc = result.document
        if self._vision is not None and self._vision.enabled:
            if result.input.format == InputFormat.IMAGE:
                _describe_whole_image(doc, path, self._vision)
            _check_descriptions(doc, self._vision)
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


def _describe_pictures(options: ConvertPipelineOptions, vision: VisionSettings) -> None:
    """Описание картинок VLM-сервером роли `vision` (OpenAI `/chat/completions`). Картинки
    меньше `min_area` страницы и классов из `skip` (логотипы, иконки...) не описываются."""
    options.enable_remote_services = True  # иначе Docling запрещает вызовы API
    options.do_picture_description = True
    options.picture_description_options = PictureDescriptionApiOptions(
        url=f"{vision.url}/chat/completions",
        headers=auth_headers(vision.api_key_env),
        params={"model": vision.model, "max_tokens": vision.max_tokens, **vision.params},
        prompt=vision.prompt,
        timeout=vision.timeout_s,
        concurrency=vision.concurrency,
        picture_area_threshold=vision.min_area,
        classification_deny=[PictureClassificationLabel(label) for label in vision.skip] or None,
        # классификатор возвращает все классы с вероятностями; без порога запрещённый класс
        # с вероятностью 0.001 отсеивал бы любую картинку
        classification_min_confidence=_SKIP_CONFIDENCE,
    )
    # классы картинок нужны только для фильтра `skip`
    options.do_picture_classification = bool(vision.skip)


def _describe_whole_image(
    doc: DoclingDocument,
    path: Path,
    vision: VisionSettings,
    client: httpx.Client | None = None,
    retry_wait=DEFAULT_RETRY_WAIT,
) -> None:
    """Файл-картинка (скриншот) описывается целиком: разметка находит в нём текст и таблицы,
    а не картинку, и до описания картинок Docling он иначе не доходит. Описание встаёт в
    начало документа, перед текстом OCR."""
    with Image.open(path) as source:
        image = source.convert("RGB")
    try:
        text = _ask_vlm(image, vision, client, retry_wait)
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise VisionError(
            f"{path.name}: the VLM at {vision.url} failed: {type(exc).__name__}: {exc}"
        ) from exc
    if not text.strip():
        raise VisionError(f"{path.name}: the VLM at {vision.url} returned an empty description")
    page = doc.pages.get(1)
    width, height = (page.size.width, page.size.height) if page else image.size
    picture = doc.add_picture(
        prov=ProvenanceItem(
            page_no=1,
            bbox=BoundingBox(l=0, t=0, r=width, b=height, coord_origin=CoordOrigin.TOPLEFT),
            charspan=(0, 0),
        )
    )
    picture.meta = PictureMeta(description=DescriptionMetaField(text=text, created_by=vision.model))
    doc.body.children.insert(0, doc.body.children.pop())


def _ask_vlm(
    image: Image.Image,
    vision: VisionSettings,
    client: httpx.Client | None = None,
    retry_wait=DEFAULT_RETRY_WAIT,
) -> str:
    """Один запрос OpenAI `/chat/completions` с картинкой (PNG в data URL) и промптом
    `vision.prompt`; сетевые ошибки и 5xx повторяются."""
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    data_url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    payload = {
        "model": vision.model,
        "max_tokens": vision.max_tokens,
        **vision.params,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": vision.prompt},
                ],
            }
        ],
    }
    http = client or httpx.Client(
        base_url=vision.url, timeout=vision.timeout_s, headers=auth_headers(vision.api_key_env)
    )
    try:
        response = post_json_with_retry(
            http, "/chat/completions", payload, attempts=3, wait=retry_wait
        )
    finally:
        if client is None:
            http.close()
    return response.json()["choices"][0]["message"]["content"] or ""


def _check_descriptions(doc: DoclingDocument, vision: VisionSettings) -> None:
    """Docling не бросает ошибки API описания (сервер VLM недоступен, таймаут), а оставляет
    описание пустым; такой разбор — ошибка стадии parse, её повторит `--retry-failed`."""
    empty = [
        p for p in doc.pictures if p.meta and p.meta.description and not p.meta.description.text
    ]
    if empty:
        raise VisionError(
            f"{len(empty)} of {len(doc.pictures)} pictures got no description from the VLM at "
            f"{vision.url} (see the log); rerun with --retry-failed"
        )


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

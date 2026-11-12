"""Стадия `parse` конвейера ingest: разбор исходного файла в DoclingDocument.

Место в потоке данных: **parse** → `chunk` (`chunking/hybrid.py`) → `embed` → `extract` →
`graph`. `DoclingParser.parse` превращает файл (PDF, DOCX, PPTX, картинки и прочие форматы
Docling) в `DoclingDocument` и сохраняет его в кэш разбора `<cache_dir>/<doc_id>.json`;
стадия `chunk` и `reindex` потом берут документ оттуда (`load`), не разбирая файл повторно.
Картинки документа пишутся PNG в `<cache_dir>/<doc_id>/images/` вместе с манифестом
`images.json`.

Что делает Docling:
- PDF и картинки идут одним конвейером (`PdfPipelineOptions`): разметка страницы, таблицы и
  OCR. Движок OCR авто (RapidOCR); `parsing.ocr_lang` выбирает его модель, и первым должен
  стоять язык с кириллицей, иначе RapidOCR берёт китайскую модель, которая кириллицу не
  читает.
- С `vision.enabled` каждая картинка внутри PDF, DOCX и PPTX отправляется на
  `/chat/completions` VLM-сервера роли `vision` (`PictureDescriptionApiOptions`), а файл-
  картинка целиком описывается отдельным запросом (`_describe_whole_image`). Описание
  становится частью текста документа, поэтому его видят чанки, граф и цитаты.

Инварианты:
- Docling глотает ошибки API описания и оставляет пустое описание; `_check_descriptions`
  превращает это в `VisionError`, чтобы стадия parse упала и её повторил `--retry-failed`, а
  не прошла молча с потерянными картинками.
- Лимиты `parsing.max_file_mb` и `parsing.max_pages` дают `ParseLimitError` до или вместо
  тяжёлого разбора.
- JSON кэша пишется атомарно (временный файл + `replace`), картинки в него не встраиваются.
"""

from __future__ import annotations

import base64
import io
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import httpx
from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
from docling.datamodel.base_models import ConversionStatus, InputFormat
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
    ImageRefMode,
    PictureItem,
    PictureMeta,
    ProvenanceItem,
)
from docling_core.types.doc.labels import PictureClassificationLabel
from PIL import Image

from mnogobase.core.config import ParsingSettings, VisionSettings
from mnogobase.core.files import write_atomic
from mnogobase.core.http import DEFAULT_RETRY_WAIT, auth_headers, post_json_with_retry
from mnogobase.core.log import get_logger

_SKIP_CONFIDENCE = 0.5  # картинка не описывается, если класс из vision.skip вероятнее этого
MAX_VLM_SIDE = 2048  # пикселей по длинной стороне картинки, которую описывает VLM


class VisionError(RuntimeError):
    """VLM не описал картинку: документ не должен считаться разобранным без описаний."""


class ParseError(RuntimeError):
    """Docling не разобрал файл."""


class ParseLimitError(ParseError):
    """Файл больше `parsing.max_pages` / `parsing.max_file_mb`."""


@dataclass
class ParsedDocument:
    """Разобранный документ и его метаданные для стадий chunk и graph.

    `title` — первый элемент с меткой TITLE или имя файла без расширения; `mime` — тип из
    Docling; `n_pages` — число страниц или None для форматов без страниц."""

    doc: DoclingDocument
    title: str
    mime: str
    n_pages: int | None


class DoclingParser:
    """Разбор файлов через Docling с кэшем результата на диске.

    Аргументы: `settings` — секция `parsing`; `device` — устройство для моделей Docling
    (`cpu`, `mps`...); `cache_dir` — каталог кэша разбора; `vision` — секция `vision` (None
    или `enabled: false` — картинки не описываются). Конвертер Docling создаётся лениво:
    загрузка его моделей тяжёлая и не нужна командам, которые не разбирают файлы."""

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
        """Создаёт (один раз) `DocumentConverter` с опциями PDF/картинок и, при включённом
        vision, с описанием картинок для PDF, картинок, DOCX и PPTX. Прочие форматы
        разбираются опциями Docling по умолчанию."""
        if self._converter is None:
            pdf_options = PdfPipelineOptions(
                do_ocr=self._s.ocr,
                ocr_options=OcrAutoOptions(lang=self._s.ocr_lang),
                generate_picture_images=True,  # картинки сохраняются в кэш разбора (images/)
                images_scale=2.0,
                accelerator_options=AcceleratorOptions(device=AcceleratorDevice(self._device)),
            )
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
        """Путь к JSON кэша разбора документа."""
        return self._cache / f"{doc_id}.json"

    def parse(self, path: Path, doc_id: str) -> ParsedDocument:
        """Разбирает файл `path` и кладёт результат в кэш под `doc_id`.

        Побочные эффекты: запуск моделей Docling (разметка, OCR), при vision — HTTP-запросы
        к VLM; запись `<doc_id>.json` и каталога `<doc_id>/images/` в кэш разбора.

        Исключения: `ParseLimitError` — файл больше `max_file_mb` или страниц больше
        `max_pages`; `ParseError` — Docling не разобрал файл; `VisionError` — VLM не описал
        одну или несколько картинок."""
        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > self._s.max_file_mb:
            raise ParseLimitError(
                f"{path.name}: {size_mb:.0f} MB is over parsing.max_file_mb={self._s.max_file_mb}"
            )
        result = self._get_converter().convert(
            path, raises_on_error=False, max_num_pages=self._s.max_pages
        )
        if result.status not in (ConversionStatus.SUCCESS, ConversionStatus.PARTIAL_SUCCESS):
            # превышение max_num_pages Docling отдаёт как неуспешную конвертацию; отличаем
            # этот случай по числу страниц входного файла, чтобы сообщить о лимите
            pages = getattr(result.input, "page_count", 0) or 0
            if pages > self._s.max_pages:
                raise ParseLimitError(
                    f"{path.name}: {pages} pages is over parsing.max_pages={self._s.max_pages}"
                )
            details = "; ".join(e.error_message for e in result.errors)[:500]
            raise ParseError(f"{path.name}: Docling failed ({result.status.value}) {details}")
        doc = result.document
        if self._vision is not None and self._vision.enabled:
            if result.input.format == InputFormat.IMAGE:
                _describe_whole_image(doc, path, self._vision)
            _check_descriptions(doc, self._vision)
        self._cache.mkdir(parents=True, exist_ok=True)
        n_images = _save_images(doc, self._cache / doc_id / "images")
        # картинки уже лежат PNG в images/, в JSON разбора они не дублируются base64
        temporary = self._cache / f".{doc_id}.{uuid4().hex}.tmp"
        try:
            doc.save_as_json(
                temporary, image_mode=ImageRefMode.PLACEHOLDER, indent=None, ensure_ascii=False
            )
            temporary.replace(self.cache_path(doc_id))
        finally:
            temporary.unlink(missing_ok=True)
        parsed = _wrap(doc, path)
        self._log.info(
            "parsed", doc_id=doc_id, title=parsed.title, n_pages=parsed.n_pages, n_images=n_images
        )
        return parsed

    def load(self, doc_id: str, path: Path) -> ParsedDocument:
        """Читает ранее разобранный документ из кэша без повторного разбора.

        `path` нужен только для запасного заголовка (имя файла). Исключения:
        `FileNotFoundError`, если кэша нет; ошибка валидации pydantic, если JSON повреждён."""
        doc = DoclingDocument.model_validate_json(
            self.cache_path(doc_id).read_text(encoding="utf-8")
        )
        return _wrap(doc, path)

    def drop_cache(self, doc_id: str) -> None:
        """Удаляет JSON разбора и сохранённые картинки документа (при удалении документа)."""
        self.cache_path(doc_id).unlink(missing_ok=True)
        shutil.rmtree(self._cache / doc_id, ignore_errors=True)


def _describe_pictures(options: ConvertPipelineOptions, vision: VisionSettings) -> None:
    """Описание картинок VLM-сервером роли `vision` (OpenAI `/chat/completions`). Картинки
    меньше `min_area` страницы и классов из `skip` (логотипы, иконки...) не описываются."""
    # меняет `options` на месте: тот же объект уже передан или будет передан FormatOption
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
    начало документа, перед текстом OCR.

    `client` и `retry_wait` подменяются в тестах. Исключения: `VisionError` при ошибке
    запроса, неожиданной форме ответа или пустом описании."""
    with Image.open(path) as source:
        image = source.convert("RGB")
    # большой скриншот VLM всё равно ужмёт, а base64 огромного PNG — лишние мегабайты запроса
    image.thumbnail((MAX_VLM_SIDE, MAX_VLM_SIDE))
    try:
        text = _ask_vlm(image, vision, client, retry_wait)
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise VisionError(
            f"{path.name}: the VLM at {vision.url} failed: {type(exc).__name__}: {exc}"
        ) from exc
    if not text.strip():
        raise VisionError(f"{path.name}: the VLM at {vision.url} returned an empty description")
    # рамка картинки — вся первая страница (у картинки она одна) или сам размер картинки
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
    # add_picture дописывает элемент в конец body; переносим его в начало
    doc.body.children.insert(0, doc.body.children.pop())


def _ask_vlm(
    image: Image.Image,
    vision: VisionSettings,
    client: httpx.Client | None = None,
    retry_wait=DEFAULT_RETRY_WAIT,
) -> str:
    """Один запрос OpenAI `/chat/completions` с картинкой (PNG в data URL) и промптом
    `vision.prompt`; сетевые ошибки и 5xx повторяются.

    Возвращает текст ответа (пустую строку, если `content` пуст). Если `client` не передан,
    создаёт временный клиент к `vision.url` и закрывает его после запроса."""
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
    описание пустым; такой разбор — ошибка стадии parse, её повторит `--retry-failed`.

    Картинки без описания вовсе (отсеянные по `min_area` или классам `skip`) ошибкой не
    считаются: проверяется только описание, которое есть, но без текста."""
    empty = [
        p for p in doc.pictures if p.meta and p.meta.description and not p.meta.description.text
    ]
    if empty:
        raise VisionError(
            f"{len(empty)} of {len(doc.pictures)} pictures got no description from the VLM at "
            f"{vision.url} (see the log); rerun with --retry-failed"
        )


def _wrap(doc: DoclingDocument, path: Path) -> ParsedDocument:
    """Собирает `ParsedDocument` из DoclingDocument: заголовок, MIME-тип, число страниц."""
    mime = doc.origin.mimetype if doc.origin else "application/octet-stream"
    return ParsedDocument(
        doc=doc, title=_title(doc, path), mime=mime, n_pages=len(doc.pages) or None
    )


def _title(doc: DoclingDocument, path: Path) -> str:
    """Текст первого непустого элемента с меткой TITLE, иначе имя файла без расширения."""
    for item, _level in doc.iterate_items():
        if getattr(item, "label", None) == DocItemLabel.TITLE and item.text.strip():
            return item.text.strip()
    return path.stem


def _save_images(doc: DoclingDocument, out_dir: Path) -> int:
    """Сохраняет картинки документа как `img_NNNN.png` в `out_dir` и пишет манифест
    `images.json` (файл, подпись, страница).

    Каталог создаётся только если есть хотя бы одна картинка с изображением; манифест
    пишется атомарно. Возвращает число сохранённых картинок."""
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
        write_atomic(out_dir / "images.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return len(manifest)

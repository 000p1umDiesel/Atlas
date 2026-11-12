"""Стадия `chunk` конвейера ingest: разбиение DoclingDocument на чанки.

Место в потоке данных: `parse` (Docling, кэш разбора в JSON) → **chunk** → `embed` → `extract`
→ `graph`. Чанкер получает `ParsedDocument` из кэша разбора и возвращает список `ChunkRecord`,
который дальше эмбеддится в Qdrant и записывается в Neo4j.

Как режется документ: `HybridChunker` из docling-core идёт по структуре документа (заголовки,
абзацы, таблицы, списки), режет слишком длинные элементы по токенам и склеивает соседние
короткие куски под одним заголовком (`merge_peers=True`), пока они укладываются в
`chunking.max_tokens`. Токены считает токенизатор той же модели, что потом эмбеддит чанки
(см. `chunking/tokenizer.py`), поэтому лимит соблюдается для реального входа эмбеддера.

Инварианты:
- `chunk_id = f"{doc_id}:{idx:05d}"` (`core/ids.py`); `idx` — порядковый номер среди
  непустых чанков, так что повторный чанкинг того же файла даёт те же id и все записи
  остаются идемпотентными upsert/MERGE.
- `text` — сам текст чанка (его видит пользователь в цитатах), `context_text` — текст вместе
  с заголовками (`contextualize`), именно он эмбеддится и по нему считается `n_tokens`.
"""

from __future__ import annotations

from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
from docling_core.transforms.chunker.tokenizer.base import BaseTokenizer

from mnogobase.chunking.tokenizer import load_tokenizer
from mnogobase.core.config import ChunkingSettings, EmbedderSettings
from mnogobase.core.ids import chunk_id
from mnogobase.core.models import ChunkRecord
from mnogobase.parsing.docling_parser import ParsedDocument


class Chunker:
    """Чанкинг с учётом структуры документа и токенов, на токенизаторе самого эмбеддера.

    Токенизатор и `HybridChunker` создаются лениво при первом `chunk`: при `tokenizer: auto`
    это сетевой запрос к серверу эмбеддера, и команды, которым чанкинг не нужен, его не
    делают."""

    def __init__(self, settings: ChunkingSettings, embedder: EmbedderSettings):
        self._s = settings
        self._embedder = embedder
        self._chunker: HybridChunker | None = None
        self._tokenizer: BaseTokenizer | None = None

    def _get(self) -> tuple[HybridChunker, BaseTokenizer]:
        """Возвращает чанкер и токенизатор, при первом вызове создавая их.

        Исключения: `TokenizerError`, если токенизатор не удаётся загрузить."""
        if self._chunker is None:
            self._tokenizer = load_tokenizer(self._s, self._embedder)
            self._chunker = HybridChunker(tokenizer=self._tokenizer, merge_peers=True)
        return self._chunker, self._tokenizer

    def close(self) -> None:
        """Закрывает HTTP-клиент токенизатора сервера эмбеддера, если он создавался.

        После закрытия следующий `chunk` создаст токенизатор заново."""
        close = getattr(self._tokenizer, "close", None)
        if close is not None:
            close()
        self._chunker = self._tokenizer = None

    def chunk(self, parsed: ParsedDocument, doc_id: str, path: str) -> list[ChunkRecord]:
        """Режет разобранный документ на чанки.

        Аргументы: `parsed` — результат `DoclingParser.parse`/`load`; `doc_id` — id
        содержимого файла (основа `chunk_id`); `path` — путь к файлу, который попадёт в
        payload чанка и в цитаты.

        Возвращает чанки по порядку; пустые (только пробелы) пропускаются и номер не
        занимают. `page_start`/`page_end` — минимальная и максимальная страница среди
        элементов документа, из которых собран чанк (None для форматов без страниц, например
        DOCX). Побочные эффекты: при `tokenizer: auto` — HTTP-запросы `/tokenize` к серверу
        эмбеддера."""
        chunker, tokenizer = self._get()
        records: list[ChunkRecord] = []
        for raw in chunker.chunk(parsed.doc):
            text = raw.text.strip()
            if not text:
                continue
            pages = sorted({p.page_no for item in raw.meta.doc_items for p in item.prov})
            context = chunker.contextualize(raw)
            idx = len(records)
            records.append(
                ChunkRecord(
                    chunk_id=chunk_id(doc_id, idx),
                    doc_id=doc_id,
                    idx=idx,
                    text=text,
                    context_text=context,
                    headings=list(raw.meta.headings or []),
                    page_start=pages[0] if pages else None,
                    page_end=pages[-1] if pages else None,
                    n_tokens=tokenizer.count_tokens(context),
                    path=path,
                )
            )
        return records

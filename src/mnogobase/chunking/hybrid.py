from __future__ import annotations

from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
from docling_core.transforms.chunker.tokenizer.base import BaseTokenizer

from mnogobase.chunking.tokenizer import load_tokenizer
from mnogobase.core.config import ChunkingSettings, EmbedderSettings
from mnogobase.core.ids import chunk_id
from mnogobase.core.models import ChunkRecord
from mnogobase.parsing.docling_parser import ParsedDocument


class Chunker:
    """Чанкинг с учётом структуры документа и токенов, на токенизаторе самого эмбеддера."""

    def __init__(self, settings: ChunkingSettings, embedder: EmbedderSettings):
        self._s = settings
        self._embedder = embedder
        self._chunker: HybridChunker | None = None
        self._tokenizer: BaseTokenizer | None = None

    def _get(self) -> tuple[HybridChunker, BaseTokenizer]:
        if self._chunker is None:
            self._tokenizer = load_tokenizer(self._s, self._embedder)
            self._chunker = HybridChunker(tokenizer=self._tokenizer, merge_peers=True)
        return self._chunker, self._tokenizer

    def close(self) -> None:
        """Закрывает HTTP-клиент токенизатора сервера эмбеддера, если он создавался."""
        close = getattr(self._tokenizer, "close", None)
        if close is not None:
            close()
        self._chunker = self._tokenizer = None

    def chunk(self, parsed: ParsedDocument, doc_id: str, path: str) -> list[ChunkRecord]:
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

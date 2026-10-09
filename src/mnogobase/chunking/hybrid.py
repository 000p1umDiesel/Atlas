from __future__ import annotations

from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer

from mnogobase.config import ChunkingSettings
from mnogobase.ids import chunk_id
from mnogobase.models import ChunkRecord
from mnogobase.parsing.docling_parser import ParsedDocument


class Chunker:
    """Чанкинг с учётом структуры документа и токенов, на токенизаторе самого эмбеддера."""

    def __init__(self, settings: ChunkingSettings):
        self._s = settings
        self._chunker: HybridChunker | None = None
        self._tokenizer: HuggingFaceTokenizer | None = None

    def _get(self) -> tuple[HybridChunker, HuggingFaceTokenizer]:
        if self._chunker is None:
            from transformers import AutoTokenizer

            self._tokenizer = HuggingFaceTokenizer(
                tokenizer=AutoTokenizer.from_pretrained(self._s.tokenizer),
                max_tokens=self._s.max_tokens,
            )
            self._chunker = HybridChunker(tokenizer=self._tokenizer, merge_peers=True)
        return self._chunker, self._tokenizer

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

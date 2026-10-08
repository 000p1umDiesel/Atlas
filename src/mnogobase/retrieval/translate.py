from __future__ import annotations

from mnogobase.llm.client import LLMClient
from mnogobase.llm.templates import render
from mnogobase.log import get_logger

# a question is translated when more than this share of its letters is not ASCII (Latin)
NON_LATIN_SHARE = 0.3


def needs_translation(question: str) -> bool:
    letters = [c for c in question if c.isalpha()]
    if not letters:
        return False
    return sum(not c.isascii() for c in letters) / len(letters) > NON_LATIN_SHARE


class QueryTranslator:
    """English phrasing of a non-English question, searched alongside the original.

    Sources, wiki pages and entity names are mostly English, and BM25 cannot match a Russian
    query to English text. A failed translation is logged and the search runs on the original
    alone. Results are memoized per question, so `compare` translates once for all modes.
    """

    def __init__(self, llm: LLMClient):
        self._llm = llm
        self._log = get_logger(__name__)
        self._cache: dict[str, list[str]] = {}

    async def alt_queries(self, question: str) -> list[str]:
        if not needs_translation(question):
            return []
        if question not in self._cache:
            self._cache[question] = await self._translate(question)
        return self._cache[question]

    async def _translate(self, question: str) -> list[str]:
        prompt = render("translate_query", question=question)
        try:
            text = await self._llm.complete([{"role": "user", "content": prompt}], task="query")
        except Exception as exc:  # the search still works without it
            self._log.warning(
                "query_translation_failed", error_type=type(exc).__name__, error=str(exc)
            )
            return []
        lines = [line for line in text.strip().splitlines() if line.strip()]
        text = lines[0].strip().strip("\"'«»“”").strip() if lines else ""
        if not text or text.casefold() == question.strip().casefold():
            return []
        self._log.info("query_translated", question=question, translation=text)
        return [text]

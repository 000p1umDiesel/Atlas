"""Английская формулировка неанглийского вопроса для поиска (`retrieval.translate`).

Сущности извлекаются под каноническими английскими именами, wiki-страницы и многие
источники тоже английские, а BM25 не сопоставит русский запрос с английским текстом.
Поэтому вопрос с заметной долей нелатинских букв переводится LLM (`task="query"`, промпт
`translate_query.md`), и перевод передаётся retriever'ам как `alt_queries` — поиск идёт по
обеим формулировкам. Отвечает LLM всё равно на исходный вопрос.
"""

from __future__ import annotations

from mnogobase.core.log import get_logger, safe_error
from mnogobase.llm.client import LLMClient
from mnogobase.llm.templates import render_messages

# вопрос переводится, если доля не-ASCII (не латинских) букв в нём больше этого порога
NON_LATIN_SHARE = 0.3


def needs_translation(question: str) -> bool:
    """Нужен ли перевод: доля не-ASCII среди букв вопроса больше `NON_LATIN_SHARE`.

    Цифры, пробелы и знаки препинания не учитываются; вопрос без букв не переводится.
    Эвристика дешёвая и не вызывает LLM: английский вопрос с парой русских слов остаётся
    как есть."""
    letters = [c for c in question if c.isalpha()]
    if not letters:
        return False
    return sum(not c.isascii() for c in letters) / len(letters) > NON_LATIN_SHARE


class QueryTranslator:
    """Английская формулировка неанглийского вопроса, по которой ищем вместе с оригиналом.

    Источники, wiki-страницы и имена сущностей в основном английские, а BM25 не сопоставит
    русский запрос с английским текстом. Неудачный перевод логируется, и поиск идёт только
    по оригиналу. Результаты мемоизируются по вопросу, так что `compare` переводит один раз
    для всех режимов.
    """

    def __init__(self, llm: LLMClient):
        self._llm = llm
        self._log = get_logger(__name__)
        # кеш без ограничения размера: экземпляр живёт в пределах одного запроса/команды
        self._cache: dict[str, list[str]] = {}

    async def alt_queries(self, question: str) -> list[str]:
        """`[перевод]` или `[]`, если перевод не нужен, не удался или совпал с вопросом.

        Пустой результат тоже кешируется, так что неудачный перевод не повторяется для
        следующих режимов. Исключений не бросает."""
        if not needs_translation(question):
            return []
        if question not in self._cache:
            self._cache[question] = await self._translate(question)
        return self._cache[question]

    async def _translate(self, question: str) -> list[str]:
        """Один вызов LLM; из ответа берётся первая непустая строка без обрамляющих кавычек."""
        messages = render_messages("translate_query", question=question)
        try:
            text = await self._llm.complete(messages, task="query")
        except Exception as exc:  # поиск работает и без него
            self._log.warning(
                "query_translation_failed", error_type=type(exc).__name__, error=safe_error(exc)
            )
            return []
        lines = [line for line in text.strip().splitlines() if line.strip()]
        text = lines[0].strip().strip("\"'«»“”").strip() if lines else ""
        if not text or text.casefold() == question.strip().casefold():
            return []
        # вопросы владельца — его данные: в журнал только на уровне DEBUG
        self._log.debug("query_translated", question=question, translation=text)
        return [text]

"""Режим `all`: объединяет контекст режимов `rag`, `wiki` и `graph` в один список.

Деление бюджета. Общий бюджет — `retrieval.context_tokens`; режим `name` получает
`int(context_tokens * retrieval.budget[name])` токенов (по умолчанию rag 0.4, wiki 0.3,
graph 0.3; сумма долей не больше 1, это проверяет конфиг). Режим без доли в `budget`
получает 0 и фактически выключен. Неизрасходованный остаток одного режима другим не
передаётся.

Каждый режим ищет с тем же `k` и теми же `alt_queries`; его элементы берутся по порядку,
пока помещаются в его долю (оценка — `estimate_tokens`). Элемент, который не влезает,
пропускается, но перебор продолжается: следующий элемент поменьше ещё может поместиться.
Дубликаты по `(kind, ref)` отбрасываются — побеждает режим, идущий раньше в `parts`
(rag → wiki → graph), так что один и тот же чанк не тратит бюджет дважды.

Итоговый порядок: сначала все элементы rag, затем wiki, затем graph. `Answerer` потом ещё
раз режет по полному `context_tokens`, но после этого деления обычно уже всё помещается.
"""

from __future__ import annotations

from collections.abc import Sequence

from mnogobase.core.models import ContextItem
from mnogobase.retrieval.base import Retriever, estimate_tokens


class CombinedRetriever:
    """Режим `all`: каждая часть получает долю бюджета токенов; элементы дедуплицируются."""

    def __init__(self, parts: dict[str, Retriever], budget: dict[str, float], max_tokens: int):
        """`parts` — retriever'ы по имени режима (порядок словаря = приоритет при
        дедупликации и порядок в выдаче); `budget` — доли `max_tokens` по тем же именам."""
        self._parts = parts
        self._budget = budget
        self._max = max_tokens

    def retrieve(self, query: str, k: int, alt_queries: Sequence[str] = ()) -> list[ContextItem]:
        """Опрашивает части по очереди и набирает их элементы в пределах долей бюджета.

        Части вызываются последовательно, исключение любой из них пробрасывается.
        """
        out: list[ContextItem] = []
        seen: set[tuple[str, str]] = set()
        for name, retriever in self._parts.items():
            limit = int(self._max * self._budget.get(name, 0.0))
            used = 0
            for item in retriever.retrieve(query, k, alt_queries):
                key = (item.kind, item.ref)
                if key in seen:
                    continue
                cost = estimate_tokens(item.text)
                if used + cost > limit:
                    continue  # следующий элемент поменьше ещё может поместиться
                seen.add(key)
                used += cost
                out.append(item)
        return out

from __future__ import annotations

from collections.abc import Sequence

from mnogobase.models import ContextItem
from mnogobase.retrieval.base import Retriever, estimate_tokens


class CombinedRetriever:
    """`all` mode: each part gets a share of the token budget; items are de-duplicated."""

    def __init__(self, parts: dict[str, Retriever], budget: dict[str, float], max_tokens: int):
        self._parts = parts
        self._budget = budget
        self._max = max_tokens

    def retrieve(self, query: str, k: int, alt_queries: Sequence[str] = ()) -> list[ContextItem]:
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
                    continue  # a smaller later item may still fit
                seen.add(key)
                used += cost
                out.append(item)
        return out

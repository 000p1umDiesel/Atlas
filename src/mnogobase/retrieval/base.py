from __future__ import annotations

from typing import Protocol

from mnogobase.models import ContextItem


class Retriever(Protocol):
    def retrieve(self, query: str, k: int) -> list[ContextItem]: ...


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)

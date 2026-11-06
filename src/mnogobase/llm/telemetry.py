"""Context-local, prompt-free accounting of individual provider attempts."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True)
class CallEvent:
    id: str
    team_id: str
    model: str
    operation: str
    created_at: float
    status: str = "started"
    tokens_in: int | None = None
    tokens_out: int | None = None
    job_id: int | None = None
    external: bool = True


_SCOPE: ContextVar[tuple[str, Callable[[CallEvent], None], int | None] | None] = ContextVar(
    "llm_usage_scope", default=None
)


@contextmanager
def usage_scope(
    team_id: str, recorder: Callable[[CallEvent], None], *, job_id: int | None = None
) -> Iterator[None]:
    token = _SCOPE.set((team_id, recorder, job_id))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def new_call(
    model: str, operation: str, *, external: bool, team_id: str | None = None
) -> CallEvent | None:
    scope = _SCOPE.get()
    if scope is None and team_id is None:
        return None
    return CallEvent(
        uuid4().hex,
        scope[0] if scope else team_id,
        model,
        operation,
        time.time(),
        job_id=scope[2] if scope else None,
        external=external,
    )


async def record_call(
    event: CallEvent | None, recorder: Callable[[CallEvent], None] | None = None
) -> None:
    scope = _SCOPE.get()
    sink = scope[1] if scope else recorder
    if sink is not None and event is not None:
        await asyncio.to_thread(sink, event)

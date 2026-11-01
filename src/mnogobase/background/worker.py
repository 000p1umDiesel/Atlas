"""Single-writer dispatch; interrupted jobs remain running for startup recovery."""

from __future__ import annotations

from pathlib import Path

from filelock import Timeout

from mnogobase.log import get_logger
from mnogobase.registry import ingest_lock

from .queue import JobQueue


async def process_one(
    queue: JobQueue, handler, data_dir: Path, *, max_attempts=3, delay=10
) -> bool:
    lock = ingest_lock(data_dir)
    try:
        lock.acquire()
    except Timeout:
        return False
    try:
        job = queue.claim()
        if job is None:
            return False
        try:
            await handler(job)
        except Exception as exc:
            # Raw provider errors can contain credentials; persist only the type.
            queue.fail(job.id, type(exc).__name__, max_attempts=max_attempts, delay=delay)
            get_logger(__name__).warning("job_failed", job_id=job.id, error_type=type(exc).__name__)
        else:
            queue.finish(job.id)
        return True
    finally:
        lock.release()

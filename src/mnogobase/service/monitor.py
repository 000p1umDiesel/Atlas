"""Independent diagnostic scan and worker heartbeat (no job mutation)."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager

from mnogobase.background.queue import JobQueue
from mnogobase.config import Settings
from mnogobase.database import connect, database_target, target_exists, transaction_lock
from mnogobase.log import get_logger
from mnogobase.service.context import team_settings
from mnogobase.service.observability import ObservabilityStore
from mnogobase.service.store import ServiceStore


@contextmanager
def worker_heartbeat(store: ObservabilityStore, interval: float):
    """A separate thread keeps ticking even during blocking parser/model calls."""
    stop = threading.Event()
    store.heartbeat(time.time())

    def run():
        while not stop.wait(interval):
            try:
                store.heartbeat(time.time())
            except Exception:
                get_logger(__name__).error("worker_heartbeat_failed")

    thread = threading.Thread(target=run, name="mnogobase-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=store.target.timeout + 1)


class ServiceMonitor:
    def __init__(self, settings: Settings, *, clock=time.time):
        self.settings = settings
        self.clock = clock
        self.service = ServiceStore(database_target(settings, "service"))
        self.store = ObservabilityStore(database_target(settings, "service"))

    def scan(self) -> dict:
        # Serialize the entire snapshot, not just reconciliation of stale snapshots.
        db = connect(self.store.target)
        try:
            with db:
                transaction_lock(db, "monitor-scan")
                return self._scan()
        finally:
            db.close()

    def _scan(self) -> dict:
        now = self.clock()
        thresholds = self.settings.service.monitor
        conditions: set[tuple[str, str, str]] = set()
        if now - self.store.worker_seen(now) >= thresholds.worker_stale_s:
            conditions.add(("", "worker_stale", "service"))
        for team in self.service.teams():
            team_id = team["id"]
            target = database_target(team_settings(self.settings, team_id), "jobs")
            if not target_exists(target, "jobs"):
                continue
            queue = JobQueue(target)
            try:
                # All unfinished jobs, not just the first UI page.
                rows = queue._db.execute(
                    "SELECT id,status,created_at,updated_at,started_at,available_at "
                    "FROM jobs WHERE status IN ('pending','running','failed')"
                )
                for row in rows:
                    status = row["status"]
                    if status == "failed":
                        kind = "job_failed"
                    elif (
                        status == "pending"
                        and now - max(row["updated_at"], row["available_at"])
                        >= thresholds.pending_s
                    ):
                        kind = "job_pending"
                    elif (
                        status == "running"
                        and now - (row["started_at"] or row["updated_at"]) >= thresholds.running_s
                    ):
                        kind = "job_running"
                    else:
                        continue
                    conditions.add((team_id, kind, str(row["id"])))
            finally:
                queue.close()
        self.store.reconcile(conditions, now=now)
        return {"checked_at": now, "active_conditions": len(conditions)}

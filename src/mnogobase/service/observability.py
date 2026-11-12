"""Учёт вызовов LLM без текста промптов: токены и оценка стоимости по `service.llm_rates`."""

from __future__ import annotations

from contextlib import contextmanager

from mnogobase.config import LLMRate
from mnogobase.database import DatabaseTarget, connect, initialize
from mnogobase.llm.telemetry import CallEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_usage (
    id TEXT PRIMARY KEY, model TEXT NOT NULL,
    operation TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL, job_id BIGINT,
    external BOOLEAN NOT NULL, status TEXT NOT NULL,
    tokens_in BIGINT, tokens_out BIGINT, currency TEXT,
    input_rate NUMERIC, output_rate NUMERIC, estimated_cost NUMERIC
);
CREATE INDEX IF NOT EXISTS llm_usage_time ON llm_usage(created_at);
"""


class UsageStore:
    def __init__(self, target: DatabaseTarget):
        self.target = target
        with self._connection() as db:
            initialize(db, _SCHEMA, "llm_usage")

    @contextmanager
    def _connection(self):
        db = connect(self.target)
        try:
            yield db
        finally:
            db.close()

    def record(self, event: CallEvent, rate: LLMRate | None = None) -> None:
        """A local attempt ID is stable across start/result and persistence retries."""
        if not event.external:
            rate = None
        with self._connection() as db, db:
            db.execute(
                "INSERT INTO llm_usage(id,model,operation,created_at,job_id,external,status,"
                "currency,input_rate,output_rate) VALUES(?,?,?,?,?,?,'started',?,?,?) "
                "ON CONFLICT(id) DO NOTHING",
                (
                    event.id,
                    event.model,
                    event.operation,
                    event.created_at,
                    event.job_id,
                    event.external,
                    rate.currency if rate else None,
                    rate.input_per_million if rate else None,
                    rate.output_per_million if rate else None,
                ),
            )
            if event.status != "started":
                db.execute(
                    "UPDATE llm_usage SET status=?,tokens_in=?,tokens_out=?,"
                    "estimated_cost=(? * input_rate + ? * output_rate)/1000000 "
                    "WHERE id=? AND status='started'",
                    (
                        event.status,
                        event.tokens_in,
                        event.tokens_out,
                        event.tokens_in,
                        event.tokens_out,
                        event.id,
                    ),
                )

    def tokens_since(self, since: float) -> int:
        """Токены (вход + выход) всех вызовов LLM начиная с `since`."""
        with self._connection() as db:
            row = db.execute(
                "SELECT COALESCE(SUM(COALESCE(tokens_in,0) + COALESCE(tokens_out,0)),0) "
                "FROM llm_usage WHERE created_at>=?",
                (since,),
            ).fetchone()
        return int(row[0])

    def summary(self, *, since: float | None = None, until: float | None = None) -> list[dict]:
        clauses, args = [], []
        for clause, value in (("created_at>=?", since), ("created_at<?", until)):
            if value is not None:
                clauses.append(clause)
                args.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connection() as db:
            rows = db.execute(
                "SELECT model,currency,external,COUNT(*) AS calls,"
                "SUM(tokens_in) AS tokens_in,SUM(tokens_out) AS tokens_out,"
                "COUNT(*) FILTER (WHERE status='error') AS errors,"
                "SUM(estimated_cost) AS estimated_cost FROM llm_usage"
                + where
                + " GROUP BY model,currency,external ORDER BY model,currency",
                tuple(args),
            ).fetchall()
        result = [dict(r) for r in rows]
        for row in result:
            for field in ("tokens_in", "tokens_out"):
                if row[field] is not None:
                    row[field] = int(row[field])
            if row["estimated_cost"] is not None:
                row["estimated_cost"] = str(row["estimated_cost"])
        return result

"""Учёт вызовов LLM без текста промптов: токены и оценка стоимости по `service.llm_rates`.

`build_app` всегда подключает `UsageStore.record` к LLM-клиенту как получателя событий
`llm/telemetry.py`: на каждую попытку вызова приходит событие `started`, затем итоговое
(`response` или `error`) с числом токенов. Задачи worker'а помечаются `job_id`
(`job_scope`). Ошибка записи не ломает сам вызов LLM — её глотает клиент.

Таблица `llm_usage` (схема проекта): одна строка на попытку; `external` — платный
внешний провайдер (для локальных моделей тарифы не применяются); тариф (`input_rate`,
`output_rate` за миллион токенов, `currency`) копируется в строку в момент вызова, поэтому
смена тарифа в конфиге не переписывает старые оценки. По этой таблице
`KnowledgeBase.check_budget` проверяет суточный лимит токенов, а `/api/status`
показывает расход за день и месяц.
"""

from __future__ import annotations

from mnogobase.core.config import LLMRate
from mnogobase.core.database import DatabaseTarget, connection, initialize
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
    """Запись и сводки по таблице `llm_usage`; каждый метод берёт короткое соединение из
    пула, так что объект можно использовать из разных потоков."""

    def __init__(self, target: DatabaseTarget):
        """Создаёт таблицу `llm_usage`, если её нет (запись в базу)."""
        self.target = target
        with connection(self.target) as db:
            initialize(db, _SCHEMA, "llm_usage")

    def record(self, event: CallEvent, rate: LLMRate | None = None) -> None:
        """Записывает событие попытки вызова LLM (одна транзакция).

        `event.id` создаётся один раз на попытку и одинаков у событий `started` и итогового,
        поэтому запись идемпотентна: `INSERT ... ON CONFLICT DO NOTHING` создаёт строку при
        первом событии (или при итоговом, если `started` потерялось), а итоговое событие
        дописывает статус, токены и `estimated_cost` только строке в статусе `started` —
        повторная доставка ничего не портит. `rate` — тариф модели из
        `service.llm_rates`; у локальных (`external=False`) вызовов он игнорируется, и
        стоимость остаётся `NULL`."""
        if not event.external:
            rate = None
        with connection(self.target) as db, db:
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
        """Сумма токенов (вход + выход) всех вызовов LLM с unix-времени `since`, включая
        локальные модели; используется для суточного лимита `service.daily_token_limit`."""
        with connection(self.target) as db:
            row = db.execute(
                "SELECT COALESCE(SUM(COALESCE(tokens_in,0) + COALESCE(tokens_out,0)),0) "
                "FROM llm_usage WHERE created_at>=?",
                (since,),
            ).fetchone()
        return int(row[0])

    def summary(self, *, since: float | None = None, until: float | None = None) -> list[dict]:
        """Сводка за полуинтервал `[since, until)` (границы необязательны), по строке на
        `(model, currency, external)`: число вызовов, ошибок, токены и оценка стоимости.

        `estimated_cost` отдаётся строкой, чтобы не терять точность `NUMERIC` в JSON;
        `None`, если тарифа нет."""
        clauses, args = [], []
        for clause, value in (("created_at>=?", since), ("created_at<?", until)):
            if value is not None:
                clauses.append(clause)
                args.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with connection(self.target) as db:
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

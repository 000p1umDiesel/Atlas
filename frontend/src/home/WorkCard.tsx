import { Link } from "@tanstack/react-router";
import { Cpu } from "lucide-react";
import { useStatus } from "../api/status";
import type { StatusPayload, UsageRow } from "../api/types";
import { ago, countOf, formatInt } from "../lib/format";

export interface CostTotal {
  currency: string;
  total: string;
}

/** Целая и дробная часть десятичной строки; `null`, если это не неотрицательное десятичное число. */
function parseDecimal(s: string): { digits: bigint; scale: number } | null {
  const m = /^(\d+)(?:\.(\d+))?$/.exec(s.trim());
  if (!m) return null;
  const frac = m[2] ?? "";
  return { digits: BigInt(m[1]! + frac), scale: frac.length };
}

/** Сумма стоимостей по валютам: целые в единицах самого мелкого знака (BigInt), без float. */
export function addCosts(rows: UsageRow[]): CostTotal[] {
  const groups = new Map<string, { digits: bigint; scale: number }[]>();
  for (const r of rows) {
    if (r.estimated_cost === null) continue;
    const parsed = parseDecimal(r.estimated_cost);
    if (!parsed) continue;
    const key = r.currency ?? "";
    groups.set(key, [...(groups.get(key) ?? []), parsed]);
  }
  return [...groups].map(([currency, values]) => {
    const scale = Math.max(...values.map((v) => v.scale));
    const sum = values.reduce((acc, v) => acc + v.digits * 10n ** BigInt(scale - v.scale), 0n);
    const text = sum.toString().padStart(scale + 1, "0");
    const total = scale === 0 ? text : `${text.slice(0, -scale)}.${text.slice(-scale)}`;
    return { currency, total };
  });
}

function workerLine(worker: StatusPayload["worker"]): string {
  if (worker.alive) return "Worker работает";
  if (worker.last_seen !== null) return `Worker не отвечает: последний раз ${ago(worker.last_seen)}`;
  return "Worker ещё ни разу не запускался";
}

const CARD = "rounded-[14px] bg-card p-5 shadow-card";

/** Главная: обработка (worker, очередь) и расход LLM за месяц. */
export function WorkCard() {
  const status = useStatus();
  if (status.isError) {
    return (
      <div className={CARD}>
        <h2 className="text-xl font-semibold tracking-tight">Обработка</h2>
        <p className="mt-2 text-sm text-muted">Сводка обработки пока недоступна</p>
      </div>
    );
  }
  const data = status.data;
  if (!data) {
    return (
      <div className={CARD}>
        <h2 className="text-xl font-semibold tracking-tight">Обработка</h2>
        <p className="mt-2 text-sm text-muted">Загружаем…</p>
      </div>
    );
  }
  const month = data.llm_usage.month;
  const calls = month.reduce((n, r) => n + r.calls, 0);
  const tokens = month.reduce((n, r) => n + (r.tokens_in ?? 0) + (r.tokens_out ?? 0), 0);
  const costs = addCosts(month);
  const unpriced = month.some((r) => r.estimated_cost === null && r.external);
  const q = data.queue;
  return (
    <div className={CARD}>
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="text-xl font-semibold tracking-tight">Обработка</h2>
        <Link to="/status" className="text-sm text-primary-ink underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-primary">
          Подробнее
        </Link>
      </div>
      <p className="mt-2 text-sm font-medium">{workerLine(data.worker)}</p>
      {data.worker.alive && data.worker.last_seen !== null && <p className="text-xs text-muted">отмечался {ago(data.worker.last_seen)}</p>}
      <p className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-sm tabular-nums">
        <span>{formatInt(q.pending ?? 0)} в очереди</span>
        <span>{formatInt(data.running.length || (q.running ?? 0))} в работе</span>
        <span>{formatInt(q.failed ?? 0)} с ошибкой</span>
      </p>
      <div className="mt-5 border-t border-line pt-4">
        <h3 className="flex items-center gap-2 text-sm font-semibold">
          <Cpu size={15} aria-hidden="true" /> Расход LLM за месяц
        </h3>
        {calls === 0 && costs.length === 0 ? (
          <p className="mt-2 text-sm text-muted">Вызовов не было.</p>
        ) : (
          <ul className="mt-2 space-y-0.5 text-sm tabular-nums">
            <li>{countOf(calls, ["вызов", "вызова", "вызовов"])}</li>
            <li>{countOf(tokens, ["токен", "токена", "токенов"])}</li>
            {costs.map((c) => (
              <li key={c.currency}>{`${c.total} ${c.currency}`.trim()}</li>
            ))}
            {unpriced && <li className="text-muted">часть вызовов без тарифа</li>}
          </ul>
        )}
      </div>
    </div>
  );
}

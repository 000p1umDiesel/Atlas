import type { UsageRow } from "../api/types";
import { formatInt } from "../lib/format";
import { TABLE, TD, TH } from "../ui/styles";
import { Block } from "./parts";

const NUM = "text-right tabular-nums";

function UsageTable({ title, rows }: { title: string; rows: UsageRow[] }) {
  return (
    <div className="min-w-0">
      <h3 className="mb-2 font-medium">{title}</h3>
      {rows.length === 0 ? (
        <p className="text-sm text-muted">Вызовов не было.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className={TABLE}>
            <thead>
              <tr>
                <th className={TH}>Модель</th>
                <th className={`${TH} ${NUM}`}>Вызовы</th>
                <th className={`${TH} ${NUM}`}>Токены</th>
                <th className={`${TH} ${NUM}`}>Ошибки</th>
                <th className={`${TH} ${NUM}`}>Оценка</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.model + (r.currency ?? "")}>
                  <td className={`${TD} [overflow-wrap:anywhere]`}>
                    {r.model}
                    {!r.external && <span className="ml-1.5 text-xs text-muted">локально</span>}
                  </td>
                  <td className={`${TD} ${NUM}`}>{formatInt(r.calls)}</td>
                  <td className={`${TD} ${NUM}`}>{formatInt((r.tokens_in ?? 0) + (r.tokens_out ?? 0))}</td>
                  <td className={`${TD} ${NUM}`}>{formatInt(r.errors)}</td>
                  <td className={`${TD} ${NUM} whitespace-nowrap`}>
                    {r.estimated_cost != null ? `${r.estimated_cost} ${r.currency ?? ""}`.trim() : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/** «Расход LLM»: сегодня (UTC) и за месяц. */
export function Usage({ today, month }: { today: UsageRow[]; month: UsageRow[] }) {
  return (
    <Block title="Расход LLM">
      <div className="grid gap-6 lg:grid-cols-2">
        <UsageTable title="Сегодня (UTC)" rows={today} />
        <UsageTable title="Этот месяц" rows={month} />
      </div>
    </Block>
  );
}

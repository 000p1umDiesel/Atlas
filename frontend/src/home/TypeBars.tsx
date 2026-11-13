import { Link } from "@tanstack/react-router";
import { motion } from "motion/react";
import type { TypeCount } from "../api/types";
import { DEFAULT_FILTERS } from "../graph/model";
import { serializeGraphSearch } from "../graph/search";
import { typeColorVar } from "../lib/entityColors";
import { countOf, formatInt } from "../lib/format";

type Props = {
  types: TypeCount[];
  /** Настроенные типы: цвет полосы = цвет типа на графе и в легенде. */
  configured: string[];
};

const ENTITY_FORMS: [string, string, string] = ["сущность", "сущности", "сущностей"];

/**
 * Распределение сущностей по типам: горизонтальные полосы от общей базовой линии,
 * по убыванию. Тип подписан текстом в каждой строке (цвет — не единственный признак),
 * число — у конца полосы, доля — в подсказке и в имени ссылки.
 */
export function TypeBars({ types, configured }: Props) {
  const rows = [...types].sort((a, b) => b.count - a.count || a.type.localeCompare(b.type));
  const total = rows.reduce((s, r) => s + r.count, 0);
  const max = rows.reduce((m, r) => Math.max(m, r.count), 0);

  if (rows.length === 0) return <p className="text-sm text-muted">Сущностей пока нет.</p>;

  return (
    <ul aria-label="Сущности по типам" className="space-y-0.5">
      {rows.map((row, i) => {
        const share = total > 0 ? Math.round((row.count / total) * 100) : 0;
        const fraction = max > 0 ? row.count / max : 0;
        const label = `${row.type}: ${countOf(row.count, ENTITY_FORMS)}, ${share}%`;
        return (
          <li key={row.type}>
            <Link
              to="/graph"
              search={serializeGraphSearch({ ...DEFAULT_FILTERS, types: [row.type], view: "2d" })}
              aria-label={label}
              title={`${label}. Показать на графе`}
              className="group -mx-2 grid grid-cols-[minmax(5.5rem,8.5rem)_1fr] items-center gap-3 rounded-lg px-2 py-1.5 transition-colors hover:bg-bg focus-visible:outline-2 focus-visible:outline-primary"
            >
              <span className="truncate text-sm">{row.type}</span>
              <span className="flex min-w-0 items-center gap-2">
                <motion.span
                  aria-hidden="true"
                  className="h-3 shrink-0 origin-left rounded-r-[4px] transition-[filter] group-hover:brightness-95"
                  style={{
                    width: `calc((100% - 4.5rem) * ${fraction})`,
                    backgroundColor: typeColorVar(row.type, configured),
                  }}
                  initial={{ scaleX: 0 }}
                  animate={{ scaleX: 1 }}
                  transition={{ delay: 0.15 + i * 0.04, duration: 0.6, ease: [0.22, 1, 0.36, 1] }}
                />
                <span className="text-sm font-medium tabular-nums">{formatInt(row.count)}</span>
                <span className="text-xs text-muted tabular-nums opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100">
                  {share}%
                </span>
              </span>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

import { motion } from "motion/react";
import { formatInt } from "../lib/format";

type Props = {
  label: string;
  value: number | null;
  /** Пояснение под числом («1 260 фрагментов»). */
  note?: string;
  /** Порядок появления (stagger). */
  index: number;
};

/** Плитка статистики: подпись, число в русской записи, пояснение. */
export function StatTile({ label, value, note, index }: Props) {
  return (
    <div role="group" aria-label={label} className="min-w-0 bg-card px-5 py-5 sm:px-6">
      {/* Анимируется содержимое, а не ячейка: фон-сетка под ней не проступает. */}
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.05 + index * 0.07, duration: 0.42, ease: [0.22, 1, 0.36, 1] }}
      >
        <p className="text-sm text-muted">{label}</p>
        <p className="mt-2 text-[2rem] leading-none font-semibold tracking-tight tabular-nums">
          {value === null ? "—" : formatInt(value)}
        </p>
        {note && <p className="mt-2 truncate text-[13px] text-muted">{note}</p>}
      </motion.div>
    </div>
  );
}

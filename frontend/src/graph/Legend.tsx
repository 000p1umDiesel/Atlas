import { ChevronDown } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useId, useState } from "react";
import { communityColorVar, typeColorVar } from "../lib/entityColors";
import { communityLabel, type CommunitySummary } from "./insights";
import type { ColorBy, SizeBy } from "./model";

const SIZE_LABEL: Record<SizeBy, string> = {
  pagerank: "PageRank",
  mentions: "число упоминаний",
  degree: "число связей",
};

type Props = {
  colorBy: ColorBy;
  /** Метрика, которая реально задаёт размер (pagerank без анализа → упоминания). */
  sizeBy: SizeBy;
  types: string[];
  /** Выбранные в фильтре типы (пусто — все): остальные в легенде приглушены. */
  activeTypes?: string[];
  communities: CommunitySummary[];
};

const MAX_COMMUNITIES = 8;

function wideScreen(): boolean {
  return typeof matchMedia !== "function" || matchMedia("(min-width: 640px)").matches;
}

/** Легенда в углу холста: цвета (типы или сообщества) и смысл размера. */
export function Legend({ colorBy, sizeBy, types, activeTypes = [], communities }: Props) {
  const [open, setOpen] = useState(wideScreen);
  const bodyId = useId();
  const items =
    colorBy === "type"
      ? types.map((t) => ({
          key: t,
          label: t,
          color: typeColorVar(t, types),
          dim: activeTypes.length > 0 && !activeTypes.includes(t),
        }))
      : communities.slice(0, MAX_COMMUNITIES).map((c) => ({
          key: c.id,
          label: communityLabel(c),
          color: communityColorVar(c.id),
          dim: false,
        }));
  const hidden = colorBy === "community" ? Math.max(0, communities.length - MAX_COMMUNITIES) : 0;

  return (
    <div className="pointer-events-auto w-60 max-w-[calc(100vw-3rem)] rounded-2xl border border-line bg-card/85 shadow-card backdrop-blur-md">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between gap-2 rounded-2xl px-3.5 py-2 text-[13px] font-medium focus-visible:outline-2 focus-visible:outline-primary"
      >
        Легенда
        <ChevronDown size={15} className={`text-muted transition-transform ${open ? "" : "-rotate-180"}`} />
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            id={bodyId}
            key="body"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.18 }}
            className="overflow-hidden"
          >
            <ul className="space-y-1 px-3.5 pb-1 text-[13px]" aria-label={colorBy === "type" ? "Цвета типов" : "Цвета сообществ"}>
              {items.map((item) => (
                <li key={item.key} className={`flex items-center gap-2 transition-opacity ${item.dim ? "opacity-40" : ""}`}>
                  <span className="size-2.5 shrink-0 rounded-full" style={{ backgroundColor: item.color }} aria-hidden="true" />
                  <span className="truncate">{item.label}</span>
                </li>
              ))}
              {hidden > 0 && <li className="text-muted">и ещё {hidden}</li>}
            </ul>
            <p className="flex items-center gap-2 border-t border-line px-3.5 py-2 text-xs text-muted">
              <span className="flex items-end gap-0.5" aria-hidden="true">
                <span className="size-1.5 rounded-full bg-muted/60" />
                <span className="size-2.5 rounded-full bg-muted/60" />
                <span className="size-3.5 rounded-full bg-muted/60" />
              </span>
              Размер узла: {SIZE_LABEL[sizeBy]}
            </p>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

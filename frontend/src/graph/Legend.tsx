import { ChevronDown } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useId, useMemo, useState } from "react";
import { communityColors, themedCss } from "../lib/communityPalette";
import { typeColorVar } from "../lib/entityColors";
import { communityLabel, type CommunitySummary } from "./insights";
import type { ColorBy, SizeBy } from "./model";

const SIZE_LABEL: Record<SizeBy, string> = {
  pagerank: "PageRank",
  mentions: "число упоминаний",
  degree: "число связей",
};

/** Пункт легенды, на который навели или нажали: тип или сообщество. */
export type LegendTarget = { kind: "type" | "community"; value: string };

type Props = {
  colorBy: ColorBy;
  /** Метрика, которая реально задаёт размер (pagerank без анализа → упоминания). */
  sizeBy: SizeBy;
  types: string[];
  /** Выбранные в фильтре типы (пусто — все): остальные в легенде приглушены. */
  activeTypes?: string[];
  /** Выбранное сообщество фильтра (null — все). */
  activeCommunity?: string | null;
  communities: CommunitySummary[];
  /** Клик по пункту: включить/выключить фильтр. Без обработчика пункты — просто текст. */
  onToggle?(target: LegendTarget): void;
  /** Наведение на пункт: подсветить его узлы на холсте (null — снять). */
  onHover?(target: LegendTarget | null): void;
};

function wideScreen(): boolean {
  return typeof matchMedia !== "function" || matchMedia("(min-width: 640px)").matches;
}

/**
 * Легенда в углу холста: все цвета (типы или каждое сообщество — со своей прокруткой)
 * и смысл размера. Пункты — кнопки-фильтры, как чипы в колонке.
 */
export function Legend({
  colorBy,
  sizeBy,
  types,
  activeTypes = [],
  activeCommunity = null,
  communities,
  onToggle,
  onHover,
}: Props) {
  const [open, setOpen] = useState(wideScreen);
  const bodyId = useId();
  const palette = useMemo(() => communityColors(communities.map((c) => c.id)), [communities]);
  const items =
    colorBy === "type"
      ? types.map((t) => ({
          key: t,
          target: { kind: "type", value: t } as LegendTarget,
          label: t,
          color: typeColorVar(t, types),
          pressed: activeTypes.includes(t),
          dim: activeTypes.length > 0 && !activeTypes.includes(t),
        }))
      : communities.map((c) => ({
          key: c.id,
          target: { kind: "community", value: c.id } as LegendTarget,
          label: communityLabel(c),
          color: themedCss(palette.get(c.id)),
          pressed: activeCommunity === c.id,
          dim: activeCommunity !== null && activeCommunity !== c.id,
        }));

  return (
    <div className="pointer-events-auto w-64 max-w-[calc(100vw-3rem)] rounded-2xl border border-line bg-card/85 shadow-card backdrop-blur-md">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between gap-2 rounded-2xl px-3.5 py-2 text-[13px] font-medium focus-visible:outline-2 focus-visible:outline-primary"
      >
        <span>
          Легенда
          {colorBy === "community" && (
            <span className="ml-1.5 font-normal text-muted tabular-nums">{communities.length}</span>
          )}
        </span>
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
            <ul
              className="legend-scroll max-h-[min(36vh,18rem)] space-y-px overflow-y-auto px-1.5 pb-1 text-[13px]"
              aria-label={colorBy === "type" ? "Цвета типов" : "Цвета сообществ"}
              onPointerLeave={() => onHover?.(null)}
            >
              {items.map((item) => {
                const body = (
                  <>
                    <span className="size-2.5 shrink-0 rounded-full" style={{ backgroundColor: item.color }} aria-hidden="true" />
                    <span className="truncate">{item.label}</span>
                  </>
                );
                return (
                  <li key={item.key} className={`transition-opacity ${item.dim ? "opacity-45" : ""}`}>
                    {onToggle ? (
                      <button
                        type="button"
                        aria-pressed={item.pressed}
                        title={item.label}
                        onClick={() => onToggle(item.target)}
                        onPointerEnter={() => onHover?.(item.target)}
                        onFocus={() => onHover?.(item.target)}
                        onBlur={() => onHover?.(null)}
                        className={`flex w-full items-center gap-2 rounded-lg px-2 py-1 text-left transition-colors hover:bg-ink/[0.06] focus-visible:outline-2 focus-visible:outline-primary ${
                          item.pressed ? "bg-ink/[0.07] font-medium" : ""
                        }`}
                      >
                        {body}
                      </button>
                    ) : (
                      <span className="flex items-center gap-2 px-2 py-1">{body}</span>
                    )}
                  </li>
                );
              })}
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

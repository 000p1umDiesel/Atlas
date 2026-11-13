import { ArrowDownLeft, ArrowUpRight, ChevronDown, Users, X } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useId, useMemo, useState } from "react";
import { communityColors, themedCss } from "../lib/communityPalette";
import { typeColorVar } from "../lib/entityColors";
import { countOf, formatInt, formatScore } from "../lib/format";
import { GRAPH_CARD_ATTR } from "./hotkeys";
import { communityLabel, topRelations, type CommunitySummary, type RelationRow } from "./insights";
import type { Graph } from "./model";

/** Ширина карточки на широком экране (страница сдвигает под неё камеру). */
export const ENTITY_CARD_WIDTH = 320;
/** Доля высоты холста под нижнюю карточку на узком экране. */
export const ENTITY_SHEET_SHARE = 0.45;
const TOP_RELATIONS = 5;

type Props = {
  graph: Graph;
  entityId: string;
  entityTypes: string[];
  communities: CommunitySummary[];
  rank: Map<string, number>;
  /** Узкий экран: нижняя выезжающая карточка вместо плавающей справа. */
  compact: boolean;
  /** Отступ сверху (под панелью инструментов холста), px; только для плавающей карточки. */
  offsetTop?: number;
  onSelect(entityId: string): void;
  onShowCommunity(community: string): void;
  onClose(): void;
};

function Metric({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div role="group" aria-label={label} className="min-w-0 px-2.5 py-2">
      <dt className="truncate text-[11px] text-muted">{label}</dt>
      <dd className="mt-0.5 truncate text-[15px] leading-tight font-semibold tabular-nums">{value}</dd>
      {note && <dd className="truncate text-[11px] leading-tight text-muted tabular-nums">{note}</dd>}
    </div>
  );
}

function RelationItem({ row, onSelect }: { row: RelationRow; onSelect(id: string): void }) {
  const Arrow = row.direction === "out" ? ArrowUpRight : ArrowDownLeft;
  return (
    <li>
      <button
        type="button"
        onClick={() => onSelect(row.otherId)}
        className="grid w-full grid-cols-[16px_minmax(0,1fr)_auto] items-center gap-2 rounded-lg px-2 py-1.5 text-left transition-colors hover:bg-ink/[0.06] focus-visible:outline-2 focus-visible:outline-primary"
      >
        <Arrow size={14} className="text-muted" aria-label={row.direction === "out" ? "исходящая" : "входящая"} />
        <span className="min-w-0">
          <span className="block truncate text-[13px] leading-snug font-medium">{row.otherName}</span>
          <span className="block truncate text-xs text-muted">{row.predicate.replaceAll("_", " ")}</span>
        </span>
        <span className="text-xs text-muted tabular-nums" title="Вес связи">
          {formatScore(row.weight)}
        </span>
      </button>
    </li>
  );
}

/**
 * Компактная карточка выбранной сущности поверх холста: имя, тип, три метрики, самые
 * весомые связи. «Подробнее» раскрывает все связи в той же карточке.
 */
export function EntityCard({
  graph,
  entityId,
  entityTypes,
  communities,
  rank,
  compact,
  offsetTop = 60,
  onSelect,
  onShowCommunity,
  onClose,
}: Props) {
  const [expanded, setExpanded] = useState(false);
  const [shownFor, setShownFor] = useState(entityId);
  const moreId = useId();
  // Другая сущность — карточка снова свёрнута.
  if (shownFor !== entityId) {
    setShownFor(entityId);
    setExpanded(false);
  }
  const relations = useMemo(() => topRelations(graph, entityId, Infinity), [graph, entityId]);
  const palette = useMemo(() => communityColors(communities.map((c) => c.id)), [communities]);
  if (!graph.hasNode(entityId)) return null;
  const a = graph.getNodeAttributes(entityId);
  const community = a.community === null ? null : (communities.find((c) => c.id === a.community) ?? null);
  const top = relations.slice(0, TOP_RELATIONS);
  const rest = relations.slice(TOP_RELATIONS);
  const place = rank.get(entityId);

  const motionProps = compact
    ? { initial: { y: "100%" }, animate: { y: 0 }, exit: { y: "100%" } }
    : { initial: { opacity: 0, x: 16, scale: 0.98 }, animate: { opacity: 1, x: 0, scale: 1 }, exit: { opacity: 0, x: 16, scale: 0.98 } };

  return (
    <motion.section
      role="dialog"
      aria-modal="false"
      aria-label={a.name}
      {...{ [GRAPH_CARD_ATTR]: "" }}
      {...motionProps}
      transition={{ type: "spring", stiffness: 420, damping: 38 }}
      className={
        compact
          ? "pointer-events-auto absolute inset-x-0 bottom-0 z-30 flex h-[45%] flex-col rounded-t-2xl border-t border-line bg-card/95 shadow-card backdrop-blur-md"
          : "pointer-events-auto absolute right-3 z-30 flex w-[320px] flex-col rounded-2xl border border-line bg-card/90 shadow-card backdrop-blur-md"
      }
      style={compact ? undefined : { top: offsetTop, maxHeight: `calc(100% - ${offsetTop + 60}px)` }}
    >
      {compact && <div className="mx-auto mt-2 h-1 w-10 shrink-0 rounded-full bg-ink/15" aria-hidden="true" />}
      <header className="flex items-start gap-2 px-4 pt-3">
        <div className="min-w-0 flex-1">
          <h2 className="line-clamp-2 font-serif text-[17px] leading-snug font-semibold">{a.name}</h2>
          <p className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
            <span className="inline-flex items-center gap-1.5 rounded-full bg-bg px-2 py-0.5 text-ink">
              <span
                className="size-2 rounded-full"
                style={{ backgroundColor: typeColorVar(a.type, entityTypes) }}
                aria-hidden="true"
              />
              {a.type}
            </span>
            {community && (
              <span className="inline-flex min-w-0 items-center gap-1.5" title={communityLabel(community)}>
                <span
                  className="size-2 shrink-0 rounded-full"
                  style={{ backgroundColor: themedCss(palette.get(community.id)) }}
                  aria-hidden="true"
                />
                <span className="truncate">сообщество из {formatInt(community.size)}</span>
              </span>
            )}
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Закрыть"
          title="Закрыть (Esc)"
          className="-mt-0.5 -mr-1.5 grid size-8 shrink-0 place-items-center rounded-full text-muted transition-colors hover:bg-ink/[0.06] hover:text-ink focus-visible:outline-2 focus-visible:outline-primary"
        >
          <X size={16} />
        </button>
      </header>

      <dl className="mx-4 mt-3 grid shrink-0 grid-cols-3 divide-x divide-line rounded-xl bg-bg/70">
        <Metric label="Упоминания" value={formatInt(a.mention_count)} />
        <Metric
          label="PageRank"
          value={a.pagerank === null ? "—" : formatScore(a.pagerank)}
          note={a.pagerank !== null && place !== undefined ? `${formatInt(place)}-е место` : undefined}
        />
        <Metric label="Связи" value={formatInt(graph.degree(entityId))} />
      </dl>

      <div className="legend-scroll mt-2 min-h-0 flex-1 overflow-y-auto px-2 pb-1">
        {relations.length === 0 ? (
          <p className="px-2 py-2 text-sm text-muted">Связей нет.</p>
        ) : (
          <>
            <ul aria-label="Связи">
              {top.map((r) => (
                <RelationItem key={r.key} row={r} onSelect={onSelect} />
              ))}
            </ul>
            <AnimatePresence initial={false}>
              {expanded && rest.length > 0 && (
                <motion.ul
                  id={moreId}
                  key="more"
                  aria-label="Остальные связи"
                  initial={{ height: 0, opacity: 0 }}
                  animate={{ height: "auto", opacity: 1 }}
                  exit={{ height: 0, opacity: 0 }}
                  transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}
                  className="overflow-hidden"
                >
                  {rest.map((r) => (
                    <RelationItem key={r.key} row={r} onSelect={onSelect} />
                  ))}
                </motion.ul>
              )}
            </AnimatePresence>
          </>
        )}
      </div>

      <footer className="flex shrink-0 flex-wrap items-center gap-2 border-t border-line px-4 py-3">
        {community && (
          <button
            type="button"
            onClick={() => onShowCommunity(community.id)}
            title={communityLabel(community)}
            className="inline-flex items-center gap-1.5 rounded-xl bg-primary-deep px-3 py-1.5 text-[13px] font-medium text-on-primary shadow-card transition-opacity hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
          >
            <Users size={14} />
            Показать сообщество
          </button>
        )}
        {rest.length > 0 && (
          <button
            type="button"
            aria-expanded={expanded}
            aria-controls={moreId}
            onClick={() => setExpanded((v) => !v)}
            className="inline-flex items-center gap-1 rounded-xl px-2.5 py-1.5 text-[13px] font-medium text-primary-ink transition-colors hover:bg-primary/15 focus-visible:outline-2 focus-visible:outline-primary"
          >
            {expanded ? "Свернуть" : `Подробнее: ещё ${countOf(rest.length, ["связь", "связи", "связей"])}`}
            <ChevronDown size={14} className={`transition-transform ${expanded ? "rotate-180" : ""}`} />
          </button>
        )}
      </footer>
    </motion.section>
  );
}

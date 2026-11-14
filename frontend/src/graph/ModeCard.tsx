import { ArrowUpDown, ChevronDown } from "lucide-react";
import { useId, useState, type ReactNode } from "react";
import { EntityPicker } from "../ui/EntityPicker";
import { Segmented } from "../ui/Segmented";
import { CommunityList } from "./CommunityList";
import type { Depth, GraphMode, GraphSearch } from "./search";

export const MODE_LABELS: Record<GraphMode, string> = {
  overview: "Обзор",
  neighbors: "Окрестность",
  path: "Путь",
  community: "Сообщество",
};

const DEPTH_OPTIONS: { value: `${Depth}`; label: string }[] = [
  { value: "1", label: "1" },
  { value: "2", label: "2" },
  { value: "3", label: "3" },
];

const HOPS_OPTIONS: { value: string; label: string }[] = ["1", "2", "3", "4", "5"].map((v) => ({ value: v, label: v }));

/** Ширина карточки режима на широком экране, px. */
export const MODE_CARD_WIDTH = 280;

type Props = {
  mode: GraphMode;
  search: GraphSearch;
  update(patch: Partial<GraphSearch>): void;
  /** Узкий экран: карточка свёрнута в заголовок режима. */
  compact: boolean;
  /** Отступ сверху (под панелью инструментов холста), px. */
  offsetTop: number;
  /** Ничего не выбрано: на узком экране карточка сразу раскрыта. */
  needsInput: boolean;
  /** Под выбором режима: найденные пути. */
  children?: ReactNode;
};

/**
 * Плавающая карточка выбора для режима (слева под панелью инструментов): сущность и глубина
 * окрестности, два конца пути и его длина (под ними — найденные пути) или список сообществ.
 * Обзору карточка не нужна.
 */
export function ModeCard({ mode, search, update, compact, offsetTop, needsInput, children }: Props) {
  const bodyId = useId();
  // null — пользователь ещё не сворачивал и не раскрывал: на узком экране раскрыто, пока выбора нет.
  const [openChoice, setOpenChoice] = useState<boolean | null>(null);
  const open = !compact || (openChoice ?? needsInput);

  let body: ReactNode;
  if (mode === "neighbors") {
    body = (
      <div className="space-y-3">
        <EntityPicker
          label="Сущность"
          value={search.center}
          onChange={(center) => update({ center, focus: center, expand: [] })}
        />
        <div className="space-y-1">
          <p className="text-[13px] font-medium text-muted" aria-hidden="true">
            Глубина
          </p>
          <Segmented
            label="Глубина"
            options={DEPTH_OPTIONS}
            value={`${search.depth}`}
            onChange={(v) => update({ depth: Number(v) as Depth })}
            layoutId="graph-depth"
            size="sm"
          />
        </div>
      </div>
    );
  } else if (mode === "path") {
    body = (
      <div className="flex min-h-0 flex-col gap-3">
        {/* Новый конец — другой путь: раскрытые соседи прежнего снимаются; обмен концов их сохраняет. */}
        <div className="space-y-2">
          <EntityPicker
            label="Откуда"
            value={search.source}
            exclude={search.target ?? undefined}
            onChange={(source) => update({ source, expand: [] })}
          />
          <div className="flex justify-center">
            <button
              type="button"
              onClick={() => update({ source: search.target, target: search.source })}
              disabled={search.source === null && search.target === null}
              className="inline-flex items-center gap-1.5 rounded-lg px-2 py-1 text-xs font-medium text-muted transition-colors hover:bg-ink/[0.06] hover:text-ink focus-visible:outline-2 focus-visible:outline-primary disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:bg-transparent"
            >
              <ArrowUpDown size={13} aria-hidden="true" />
              Поменять местами
            </button>
          </div>
          <EntityPicker
            label="Куда"
            value={search.target}
            exclude={search.source ?? undefined}
            onChange={(target) => update({ target, expand: [] })}
          />
        </div>
        <div className="space-y-1">
          <p className="text-[13px] font-medium text-muted" aria-hidden="true">
            Длина пути
          </p>
          <Segmented
            label="Длина пути"
            options={HOPS_OPTIONS}
            value={`${search.hops}`}
            onChange={(v) => update({ hops: Number(v) })}
            layoutId="graph-hops"
            size="sm"
          />
        </div>
        {children && <div className="legend-scroll -mx-1 min-h-0 overflow-y-auto border-t border-line pt-2">{children}</div>}
      </div>
    );
  } else if (mode === "community") {
    body = (
      <div className="legend-scroll -mx-1 min-h-0 overflow-y-auto">
        <CommunityList selected={search.community} onSelect={(community) => update({ community, expand: [] })} />
      </div>
    );
  } else {
    return null;
  }

  const title = MODE_LABELS[mode];
  return (
    <section
      aria-label={`Режим «${title}»`}
      className="pointer-events-auto absolute left-2.5 z-20 flex flex-col rounded-2xl border border-line bg-card/90 shadow-card backdrop-blur-md sm:left-3"
      style={{
        top: offsetTop,
        width: compact ? (open ? "calc(100% - 20px)" : undefined) : MODE_CARD_WIDTH,
        maxHeight: `calc(100% - ${offsetTop + 72}px)`,
      }}
    >
      {compact ? (
        <button
          type="button"
          aria-expanded={open}
          aria-controls={bodyId}
          onClick={() => setOpenChoice(!open)}
          className="flex items-center gap-1.5 rounded-2xl px-3 py-2 text-left text-[13px] font-semibold focus-visible:outline-2 focus-visible:outline-primary"
        >
          {title}
          <ChevronDown size={14} className={`text-muted transition-transform ${open ? "rotate-180" : ""}`} />
        </button>
      ) : (
        <h2 className="px-3.5 pt-3 font-sans text-[13px] font-semibold">{title}</h2>
      )}
      <div id={bodyId} hidden={!open} className="flex min-h-0 flex-col px-3.5 pt-2 pb-3">
        {body}
      </div>
    </section>
  );
}

import { ChevronDown } from "lucide-react";
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
};

/**
 * Плавающая карточка выбора для режима (слева под панелью инструментов): сущность и глубина
 * окрестности или список сообществ. Обзору карточка не нужна; путь добавится отдельно.
 */
export function ModeCard({ mode, search, update, compact, offsetTop, needsInput }: Props) {
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

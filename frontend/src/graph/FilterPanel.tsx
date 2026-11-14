import { PanelLeftClose, RotateCcw, Search, X } from "lucide-react";
import { useId, useMemo, useState, type CSSProperties, type ReactNode } from "react";
import { useTypeCatalog } from "../api/queries";
import { formatInt, formatScore, plural } from "../lib/format";
import { useShellColumn } from "../shell/columnContext";
import { HIGHLIGHT_FIELD_ATTR, requestFlyToMatch } from "./commands";
import { communityLabel, communitySummaries, predicateCounts, predicateLabel, shownTypes, typeCounts } from "./insights";
import { DEFAULT_FILTERS, visibleSubgraph, type ColorBy, type SizeBy } from "./model";
import { fromSlider, sliderScale, toSlider } from "./sliderScale";
import { activeMode, useGraphData } from "./useGraphData";
import { useDebouncedField, useGraphSearch } from "./useGraphSearch";
import { Segmented } from "../ui/Segmented";
import { Toggle } from "../ui/Toggle";
import { TypeHint } from "../ui/TypeHint";

const COLOR_OPTIONS: { value: ColorBy; label: string }[] = [
  { value: "type", label: "Тип" },
  { value: "community", label: "Сообщество" },
];

const SIZE_OPTIONS: { value: SizeBy; label: string }[] = [
  { value: "pagerank", label: "PageRank" },
  { value: "mentions", label: "Упоминания" },
  { value: "degree", label: "Связи" },
];

/** Вес связи: целые (суммы сил) без округления до значащих цифр, дробные — до трёх. */
const formatWeight = (v: number) => (Number.isInteger(v) ? formatInt(v) : formatScore(v));

/** Доля заполненной дорожки ползунка (CSS-переменная через CSSOM, не атрибут style в HTML). */
const fill = (percent: number) => ({ "--fill": `${percent}%` }) as CSSProperties;

/** Чипов предикатов без «Ещё N». */
const PREDICATES_SHOWN = 12;

const CHIP = "inline-flex items-center gap-1.5 rounded-full border py-1 text-[13px] transition-[background-color,border-color,opacity] focus-visible:outline-2 focus-visible:outline-primary";
const chipState = (pressed: boolean, dimmed: boolean) =>
  pressed
    ? "border-primary bg-primary/15 text-ink"
    : dimmed
      ? "border-line bg-card/50 text-muted hover:text-ink"
      : "border-line bg-card text-ink hover:border-primary/60";

function Section({ title, aside, children }: { title: string; aside?: ReactNode; children: ReactNode }) {
  const titleId = useId();
  return (
    <section role="group" aria-labelledby={titleId} className="space-y-2.5">
      <div className="flex items-baseline justify-between gap-2">
        <h3 id={titleId} className="font-sans text-[13px] font-medium text-muted">
          {title}
        </h3>
        {aside}
      </div>
      {children}
    </section>
  );
}

/** Фильтры графа (контекстная колонка) для графа любого режима. Каждое изменение пишется в URL. */
export function FilterPanel() {
  const { search, update, reset } = useGraphSearch();
  const data = useGraphData(search);
  // В режиме «Сообщество» сообщество выбирается в карточке режима, а не фильтром.
  const communityMode = activeMode(search) === "community";
  const catalog = useTypeCatalog();
  const column = useShellColumn();
  const ids = {
    highlight: useId(),
    highlightHint: useId(),
    weight: useId(),
    mentions: useId(),
    community: useId(),
  };

  const graph = data.state === "ready" ? data.graph : null;
  const counts = useMemo(() => (graph ? typeCounts(graph, search) : new Map<string, number>()), [graph, search]);
  // Чипы — только типы с узлами в текущем графе (и выбранные: их можно снять).
  const types = useMemo(() => shownTypes(catalog.order, graph, search.types), [catalog.order, graph, search.types]);
  const predicates = useMemo(() => (graph ? predicateCounts(graph, search) : []), [graph, search]);
  const [allPredicates, setAllPredicates] = useState(false);
  // Свёрнуто: первые 12 и выбранные из остальных (выбранный чип не прячется).
  const shownPredicates = allPredicates
    ? predicates
    : predicates.filter((p, i) => i < PREDICATES_SHOWN || search.predicates.includes(p.predicate));
  const hiddenPredicates = predicates.length - shownPredicates.length;
  const visible = useMemo(() => (graph ? visibleSubgraph(graph, search) : null), [graph, search]);
  const communities = useMemo(() => (graph ? communitySummaries(graph) : []), [graph]);
  // Диапазоны ползунков — из данных: вес связи (сумма сил по фрагментам) сверху не ограничен.
  const mentionScale = useMemo(() => sliderScale(graph ? graph.mapNodes((_, a) => a.mention_count) : []), [graph]);
  const weightScale = useMemo(() => sliderScale(graph ? graph.mapEdges((_, a) => a.weight) : []), [graph]);

  const [highlight, setHighlight] = useDebouncedField(search.highlight, (v) => update({ highlight: v }, { replace: true }));
  const [minWeight, setMinWeight] = useDebouncedField(search.minWeight, (v) => update({ minWeight: v }, { replace: true }));
  const [minMentions, setMinMentions] = useDebouncedField(search.minMentions, (v) =>
    update({ minMentions: v }, { replace: true }),
  );

  const isDefault =
    search.types.length === 0 &&
    (communityMode || search.community === null) &&
    search.predicates.length === 0 &&
    search.minWeight === DEFAULT_FILTERS.minWeight &&
    search.minMentions === DEFAULT_FILTERS.minMentions &&
    !search.hideIsolated &&
    search.colorBy === DEFAULT_FILTERS.colorBy &&
    search.sizeBy === DEFAULT_FILTERS.sizeBy &&
    search.highlight === "";

  const toggleType = (type: string) => {
    const next = search.types.includes(type) ? search.types.filter((t) => t !== type) : [...search.types, type];
    update({ types: next });
  };
  const togglePredicate = (predicate: string) => {
    const next = search.predicates.includes(predicate)
      ? search.predicates.filter((p) => p !== predicate)
      : [...search.predicates, predicate];
    update({ predicates: next });
  };

  return (
    <div className="space-y-6 pb-4">
      <header className="flex items-start justify-between gap-2">
        <div>
          <h2 className="text-lg font-semibold">Фильтры</h2>
          {graph && visible && (
            <p className="mt-0.5 text-sm text-muted" aria-live="polite">
              {formatInt(visible.nodes.size)} из {formatInt(graph.order)}{" "}
              {plural(graph.order, ["сущности", "сущностей", "сущностей"])}
            </p>
          )}
        </div>
        <button
          type="button"
          onClick={() => column.setCollapsed(true)}
          aria-label="Свернуть фильтры"
          title="Свернуть фильтры"
          className="hidden size-9 place-items-center rounded-full text-muted transition-colors hover:bg-card hover:text-ink focus-visible:outline-2 focus-visible:outline-primary md:grid"
        >
          <PanelLeftClose size={18} />
        </button>
      </header>

      <div className="relative">
        <label htmlFor={ids.highlight} className="sr-only">
          Подсветить по имени
        </label>
        <Search size={16} className="pointer-events-none absolute top-1/2 left-3 -translate-y-1/2 text-muted" />
        <input
          id={ids.highlight}
          type="search"
          value={highlight}
          onChange={(e) => setHighlight(e.target.value)}
          onKeyDown={(e) => {
            // Enter: подсветка сразу в URL и перелёт к самому важному совпадению.
            if (e.key !== "Enter" || e.nativeEvent.isComposing) return;
            e.preventDefault();
            update({ highlight }, { replace: true });
            requestFlyToMatch(highlight);
          }}
          {...{ [HIGHLIGHT_FIELD_ATTR]: "" }}
          aria-describedby={ids.highlightHint}
          placeholder="Подсветить по имени"
          autoComplete="off"
          className="w-full rounded-xl border border-line bg-card py-2 pr-9 pl-9 text-sm shadow-card outline-none placeholder:text-muted focus:border-primary [&::-webkit-search-cancel-button]:hidden"
        />
        {highlight !== "" && (
          <button
            type="button"
            onClick={() => setHighlight("")}
            aria-label="Очистить подсветку"
            className="absolute top-1/2 right-2 grid size-6 -translate-y-1/2 place-items-center rounded-full text-muted hover:text-ink"
          >
            <X size={14} />
          </button>
        )}
        <p id={ids.highlightHint} className="sr-only">
          Enter — перейти к самому важному совпадению
        </p>
      </div>

      <Section
        title="Типы"
        aside={
          search.types.length > 0 && (
            <button
              type="button"
              onClick={() => update({ types: [] })}
              className="text-[13px] text-primary-ink underline-offset-2 hover:underline"
            >
              Все типы
            </button>
          )
        }
      >
        <div className="flex flex-wrap gap-1.5">
          {types.length === 0 && <p className="text-[13px] text-muted">Типов на графе пока нет.</p>}
          {types.map((type) => {
            const pressed = search.types.includes(type);
            const dimmed = search.types.length > 0 && !pressed;
            const info = catalog.typeInfo(type);
            const color = catalog.typeColor(type);
            return (
              <TypeHint key={type} info={info} color={color}>
                {(hint) => (
                  <button
                    {...hint}
                    type="button"
                    aria-pressed={pressed}
                    onClick={() => toggleType(type)}
                    className={`${CHIP} pr-2.5 pl-2 ${chipState(pressed, dimmed)}`}
                  >
                    <span
                      className={`size-2.5 rounded-full ${dimmed ? "opacity-50" : ""}`}
                      style={{ backgroundColor: color }}
                      aria-hidden="true"
                    />
                    {info.label}{" "}
                    <span className="text-muted tabular-nums">{formatInt(counts.get(type) ?? 0)}</span>
                  </button>
                )}
              </TypeHint>
            );
          })}
        </div>
      </Section>

      {!communityMode && (
        <Section title="Сообщество">
          <label htmlFor={ids.community} className="sr-only">
            Сообщество
          </label>
          <select
            id={ids.community}
            value={search.community ?? ""}
            onChange={(e) => update({ community: e.target.value === "" ? null : e.target.value })}
            className="w-full rounded-xl border border-line bg-card px-3 py-2 text-sm shadow-card outline-none focus:border-primary"
          >
            <option value="">Все сообщества</option>
            {communities.map((c) => (
              <option key={c.id} value={c.id}>
                {communityLabel(c)}
              </option>
            ))}
          </select>
        </Section>
      )}

      {predicates.length > 0 && (
        <Section
          title="Связи"
          aside={
            search.predicates.length > 0 && (
              <button
                type="button"
                onClick={() => update({ predicates: [] })}
                className="text-[13px] text-primary-ink underline-offset-2 hover:underline"
              >
                Все связи
              </button>
            )
          }
        >
          <div className="flex flex-wrap gap-1.5">
            {shownPredicates.map(({ predicate, count }) => {
              const pressed = search.predicates.includes(predicate);
              const dimmed = search.predicates.length > 0 && !pressed;
              return (
                <button
                  key={predicate}
                  type="button"
                  aria-pressed={pressed}
                  onClick={() => togglePredicate(predicate)}
                  title={predicate}
                  className={`${CHIP} max-w-full px-2.5 ${chipState(pressed, dimmed)}`}
                >
                  <span className="truncate">{predicateLabel(predicate)}</span>{" "}
                  <span className="text-muted tabular-nums">{formatInt(count)}</span>
                </button>
              );
            })}
            {(hiddenPredicates > 0 || allPredicates) && predicates.length > PREDICATES_SHOWN && (
              <button
                type="button"
                aria-expanded={allPredicates}
                onClick={() => setAllPredicates((v) => !v)}
                className="rounded-full px-2.5 py-1 text-[13px] text-primary-ink underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-primary"
              >
                {allPredicates ? "Свернуть" : `Ещё ${formatInt(hiddenPredicates)}`}
              </button>
            )}
          </div>
        </Section>
      )}

      <div className="space-y-4">
        <div className="space-y-1.5">
          <div className="flex items-baseline justify-between text-[13px]">
            <label htmlFor={ids.weight} className="font-medium text-muted">
              Мин. вес связи
            </label>
            <span className="tabular-nums">{formatWeight(minWeight)}</span>
          </div>
          <input
            id={ids.weight}
            type="range"
            min={0}
            max={100}
            step={1}
            value={toSlider(minWeight, weightScale)}
            aria-valuetext={formatWeight(minWeight)}
            onChange={(e) => setMinWeight(fromSlider(Number(e.target.value), weightScale))}
            className="range w-full"
            style={fill(toSlider(minWeight, weightScale))}
          />
        </div>
        <div className="space-y-1.5">
          <div className="flex items-baseline justify-between text-[13px]">
            <label htmlFor={ids.mentions} className="font-medium text-muted">
              Мин. упоминаний
            </label>
            <span className="tabular-nums">{formatInt(minMentions)}</span>
          </div>
          <input
            id={ids.mentions}
            type="range"
            min={0}
            max={100}
            step={1}
            value={toSlider(minMentions, mentionScale)}
            aria-valuetext={formatInt(minMentions)}
            onChange={(e) => setMinMentions(fromSlider(Number(e.target.value), mentionScale))}
            className="range w-full"
            style={fill(toSlider(minMentions, mentionScale))}
          />
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={search.hideIsolated}
          onClick={() => update({ hideIsolated: !search.hideIsolated })}
          className="flex w-full items-center justify-between gap-3 rounded-xl py-1 text-sm focus-visible:outline-2 focus-visible:outline-primary"
        >
          Скрыть одиночные
          <Toggle on={search.hideIsolated} />
        </button>
      </div>

      <Section title="Цвет по">
        <Segmented
          label="Цвет по"
          options={COLOR_OPTIONS}
          value={search.colorBy}
          onChange={(colorBy) => update({ colorBy })}
          layoutId="filter-color"
        />
      </Section>
      <Section title="Размер по">
        <Segmented
          label="Размер по"
          options={SIZE_OPTIONS}
          value={search.sizeBy}
          onChange={(sizeBy) => update({ sizeBy })}
          layoutId="filter-size"
        />
      </Section>

      <button
        type="button"
        onClick={reset}
        disabled={isDefault}
        className="flex w-full items-center justify-center gap-2 rounded-xl border border-line px-3 py-2 text-sm text-ink transition-colors hover:bg-card disabled:cursor-default disabled:opacity-45 disabled:hover:bg-transparent"
      >
        <RotateCcw size={15} />
        Сбросить фильтры
      </button>
    </div>
  );
}

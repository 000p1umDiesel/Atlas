import { Download, Keyboard, Maximize2, Minus, Plus, RotateCw, SlidersHorizontal } from "lucide-react";
import { AnimatePresence, useReducedMotion } from "motion/react";
import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { ApiError, isUnauthorized } from "../api/client";
import { useCommunities } from "../api/entities";
import type { PathView } from "../api/types";
import { fixturesMode, useTypeCatalog } from "../api/queries";
import { focusHighlightField, onFlyToMatch } from "../graph/commands";
import { ENTITY_CARD_WIDTH, ENTITY_SHEET_SHARE, EntityCard } from "../graph/EntityCard";
import { Graph2D, type Graph2DApi } from "../graph/Graph2D";
import { firstMatch, graphHotkey, type GraphAction } from "../graph/hotkeys";
import { communitySummaries, rankByPagerank, shownTypes } from "../graph/insights";
import { Legend, type LegendTarget } from "../graph/Legend";
import { COMMUNITIES_LIMIT } from "../graph/CommunityList";
import { MODE_LABELS, ModeCard } from "../graph/ModeCard";
import { PathList } from "../graph/PathList";
import { sizeMetric, visibleSubgraph } from "../graph/model";
import { EXPAND_LIMIT, type GraphMode, type GraphView } from "../graph/search";
import { activeMode, useGraphData } from "../graph/useGraphData";
import { useGraphSearch } from "../graph/useGraphSearch";
import { countOf, formatInt, plural } from "../lib/format";
import { useShellColumn } from "../shell/columnContext";
import { ErrorBoundary, ErrorNotice } from "../ui/ErrorNotice";
import { Segmented } from "../ui/Segmented";
import { Toggle } from "../ui/Toggle";

// three.js — отдельный ленивый чанк: грузится только при переходе в 3D.
const Graph3D = lazy(() => import("../graph/Graph3D"));

/** Задержка, после которой граф вписывается в экран при смене состава. */
const FIT_DELAY_MS = 200;

const VIEW_OPTIONS: { value: GraphView; label: string }[] = [
  { value: "2d", label: "2D" },
  { value: "3d", label: "3D" },
];

const MODES: GraphMode[] = ["overview", "neighbors", "path", "community"];

const ENTITY_FORMS: [string, string, string] = ["сущность", "сущности", "сущностей"];

/** Итог раскрытия узла: сколько сущностей добавилось на граф. */
const addedNotice = (added: number) =>
  added > 0 ? `${plural(added, ["Добавлена", "Добавлено", "Добавлено"])} ${countOf(added, ENTITY_FORMS)}` : "Новых связей нет";

const notFound = (error: unknown) => error instanceof ApiError && error.status === 404;

/** Узлы пути для подсветки на графе. */
const pathNodes = (path: PathView | undefined): ReadonlySet<string> | null =>
  path ? new Set(path.nodes.map((n) => n.entity_id)) : null;

const KEYS: [string, string][] = [
  ["F", "вписать граф в экран"],
  ["+  −", "приблизить и отдалить"],
  ["2  3", "вид 2D или 3D"],
  ["/", "подсветить по имени"],
  ["Esc", "снять выделение"],
  ["?", "эта подсказка"],
];

const GLASS = "rounded-2xl border border-line bg-card/85 shadow-card backdrop-blur-md";
const TOOL =
  "inline-flex h-9 items-center gap-1.5 rounded-xl px-2.5 text-[13px] font-medium text-ink transition-colors hover:bg-ink/[0.06] focus-visible:outline-2 focus-visible:outline-primary disabled:cursor-not-allowed disabled:text-muted disabled:opacity-60 disabled:hover:bg-transparent";
const ICON_TOOL = `${TOOL} w-9 justify-center px-0`;

function CanvasMessage({ children }: { children: ReactNode }) {
  return <div className="pointer-events-none absolute inset-0 grid place-items-center p-6 text-center">{children}</div>;
}

const CANVAS_BUTTON =
  "pointer-events-auto rounded-xl bg-primary-deep px-4 py-2 text-sm text-on-primary shadow-card transition-opacity hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary";

const WIDE = "(min-width: 640px)";
function subscribeWide(cb: () => void) {
  if (typeof matchMedia !== "function") return () => {};
  const mq = matchMedia(WIDE);
  mq.addEventListener("change", cb);
  return () => mq.removeEventListener("change", cb);
}
const getWide = () => typeof matchMedia !== "function" || matchMedia(WIDE).matches;

/**
 * Граф: холст 2D/3D во всю область, плавающие панели, легенда, карточка сущности. Режимы —
 * обзор, окрестность сущности, путь между двумя сущностями, сообщество (данные режима — `useGraphData`).
 */
export function GraphPage() {
  const { search, update, setMode, reset } = useGraphSearch();
  const mode = activeMode(search);
  const live = !fixturesMode();
  const data = useGraphData(search);
  const catalog = useTypeCatalog();
  const column = useShellColumn();
  const reducedMotion = useReducedMotion() ?? false;
  const wide = useSyncExternalStore(subscribeWide, getWide, () => true);
  const modeHint = useId();
  const keysId = useId();
  const canvasRef = useRef<HTMLDivElement>(null);

  const graph = data.state === "ready" ? data.graph : null;
  const configured = catalog.order;
  const visible = useMemo(() => (graph ? visibleSubgraph(graph, search) : null), [graph, search]);
  const communities = useMemo(() => (graph ? communitySummaries(graph) : []), [graph]);
  // В легенде — только типы с узлами в текущем графе (и выбранные фильтром).
  const legendTypes = useMemo(() => shownTypes(configured, graph, search.types), [configured, graph, search.types]);
  // Место по PageRank имеет смысл только среди всего графа (обзор), не в окрестности или сообществе.
  const rank = useMemo(
    () => (graph && mode === "overview" ? rankByPagerank(graph) : new Map<string, number>()),
    [graph, mode],
  );

  // Выбранный узел живёт в URL (`focus`): переживает смену 2D↔3D, перезагрузку и ссылку.
  // Запись — заменой текущей записи истории, не новой на каждый клик.
  const selected = search.focus;
  const setSelected = useCallback((id: string | null) => update({ focus: id }, { replace: true }), [update]);
  const [autoRotate, setAutoRotate] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [notice, setNotice] = useState("");
  const [keysOpen, setKeysOpen] = useState(false);

  // Ручка рендерера помечена видом, которому принадлежит: после переключения 2D↔3D
  // старая ручка сразу перестаёт считаться текущей (3D без WebGL onReady не вызовет).
  const [renderer, setRenderer] = useState<{ view: GraphView; api: Graph2DApi } | null>(null);
  // Новый объект — новый рендерер (новый граф или вид): по нему камера заново вписывает граф.
  const current = renderer?.view === search.view ? renderer : null;
  const api = current?.api ?? null;
  const apiRef = useRef<Graph2DApi | null>(null);
  // До отрисовки: клавиши и легенда должны видеть ручку сразу, как только кнопки включились.
  useLayoutEffect(() => {
    apiRef.current = api;
  }, [api]);

  const onReady2d = useCallback((next: Graph2DApi) => setRenderer({ view: "2d", api: next }), []);
  const onReady3d = useCallback((next: Graph2DApi) => setRenderer({ view: "3d", api: next }), []);

  const selectedRef = useRef(selected);
  useLayoutEffect(() => {
    selectedRef.current = selected;
  }, [selected]);
  const visibleRef = useRef(visible);
  useEffect(() => {
    visibleRef.current = visible;
  }, [visible]);
  // Наведение на выбранный узел (задаётся ниже, когда известна ширина под карточку).
  const focusRef = useRef<((id: string) => void) | null>(null);

  // Через паузу после смены состава или вида: выбранный узел остался видимым — камера снова
  // на нём (его окрестность могла измениться), иначе — вписать видимые узлы.
  const fitTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const scheduleFit = useCallback(() => {
    clearTimeout(fitTimer.current);
    fitTimer.current = setTimeout(() => {
      const id = selectedRef.current;
      if (id !== null && visibleRef.current?.nodes.has(id) && focusRef.current) focusRef.current(id);
      else apiRef.current?.fit();
    }, FIT_DELAY_MS);
  }, []);
  useEffect(() => () => clearTimeout(fitTimer.current), []);

  // Sigma масштабирует кадр по всем узлам, включая скрытые: после смены состава вписываем видимые.
  const compositionKey = graph
    ? [
        search.types.join(","),
        search.community,
        search.predicates.join(","),
        search.minWeight,
        search.minMentions,
        search.hideIsolated,
      ].join("|")
    : "";
  const firstComposition = useRef(true);
  useEffect(() => {
    if (compositionKey === "") return;
    if (firstComposition.current) {
      firstComposition.current = false;
      return;
    }
    scheduleFit();
  }, [compositionKey, scheduleFit]);

  // Новый рендерер (смена вида, новая основная часть графа: центр, сообщество, путь) — вписать
  // после первых кадров. Выбранный узел виден (в том числе только что раскрытый) — нет: камера
  // сразу встаёт на него (ниже), без промежуточного кадра «весь граф».
  useEffect(() => {
    if (!current) return;
    const id = selectedRef.current;
    if (id !== null && visibleRef.current?.nodes.has(id)) return;
    scheduleFit();
  }, [current, scheduleFit]);

  // Выбранный узел — в центр свободной части холста, не под карточкой.
  const focusOn = useCallback(
    (id: string) => {
      const current = apiRef.current;
      if (!current) return;
      clearTimeout(fitTimer.current);
      const height = canvasRef.current?.clientHeight ?? 0;
      const offset = wide ? { x: (ENTITY_CARD_WIDTH + 12) / 2, y: 0 } : { x: 0, y: (height * ENTITY_SHEET_SHARE) / 2 };
      current.focusNode?.(id, offset);
    },
    [wide],
  );
  useLayoutEffect(() => {
    focusRef.current = focusOn;
  }, [focusOn]);
  useEffect(() => {
    if (current && selected !== null) focusOn(selected);
  }, [current, selected, focusOn]);

  // Выбранный узел скрыт фильтрами (или такого id нет) — выделение снимается, focus уходит из URL.
  useEffect(() => {
    if (selected !== null && visible && !visible.nodes.has(selected)) setSelected(null);
  }, [selected, visible, setSelected]);

  // Раскрытие соседей (двойной клик, «Показать связи»): id — в конец `expand` (последние 20,
  // старые вытесняются), узел выбран — камера остаётся на нём. Узлы графа до раскрытия
  // запоминаются, чтобы по приходу ответа сказать, сколько добавилось.
  const [expanding, setExpanding] = useState<{ id: string; before: ReadonlySet<string> } | null>(null);
  const expandNode = useCallback(
    (id: string) => {
      setExpanding({ id, before: new Set(graph?.nodes() ?? []) });
      const expand = [...search.expand.filter((x) => x !== id), id].slice(-EXPAND_LIMIT);
      update({ expand, focus: id }, { replace: true });
    },
    [graph, search.expand, update],
  );
  const expandStatus = expanding && data.state === "ready" ? data.expansions.get(expanding.id) : undefined;
  useEffect(() => {
    if (!expanding || !graph || expandStatus === undefined || expandStatus === "pending") return;
    setNotice(
      expandStatus === "error"
        ? "Не удалось загрузить связи. Попробуйте ещё раз."
        : addedNotice(graph.filterNodes((id) => !expanding.before.has(id)).length),
    );
    setExpanding(null);
  }, [expanding, expandStatus, graph]);

  // Карточка — под панелью инструментов, даже когда та переносится на вторую строку.
  const toolbarRef = useRef<HTMLDivElement>(null);
  const [cardTop, setCardTop] = useState(60);
  useLayoutEffect(() => {
    const el = toolbarRef.current;
    if (!el) return;
    const measure = () => setCardTop(Math.round(el.offsetTop + el.offsetHeight + 8));
    measure();
    if (typeof ResizeObserver !== "function") return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const switchView = useCallback(
    (view: GraphView) => {
      if (view === search.view) return;
      apiRef.current = null;
      update({ view });
    },
    [search.view, update],
  );

  // Enter в поле подсветки: самое важное видимое совпадение — выбрать и перелететь к нему.
  useEffect(
    () =>
      onFlyToMatch((query) => {
        const nodes = visibleRef.current?.nodes;
        if (!graph || !nodes) return;
        const id = firstMatch(graph, nodes, query);
        if (!id) setNotice("Совпадений среди видимых сущностей нет.");
        // Уже выбран — выбор не меняется, но перелететь к нему всё равно нужно.
        else if (id === selectedRef.current) focusOn(id);
        else setSelected(id);
      }),
    [graph, focusOn, setSelected],
  );

  // Горячие клавиши страницы графа.
  const runAction = useCallback(
    (action: GraphAction) => {
      switch (action) {
        case "fit":
          apiRef.current?.fit();
          break;
        case "zoomIn":
          apiRef.current?.zoomIn?.();
          break;
        case "zoomOut":
          apiRef.current?.zoomOut?.();
          break;
        case "view2d":
          switchView("2d");
          break;
        case "view3d":
          switchView("3d");
          break;
        case "search":
          if (!focusHighlightField()) {
            column.open();
            requestAnimationFrame(() => requestAnimationFrame(() => focusHighlightField()));
          }
          break;
        case "help":
          setKeysOpen((v) => !v);
          break;
        case "escape":
          if (keysOpen) setKeysOpen(false);
          else setSelected(null);
          break;
      }
    },
    [column, keysOpen, switchView, setSelected],
  );
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const action = graphHotkey(e);
      if (!action) return;
      // Esc в поле ввода — только снять фокус, выделение остаётся.
      if (action === "escape" && e.target instanceof HTMLInputElement) {
        e.target.blur();
        return;
      }
      e.preventDefault();
      runAction(action);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [runAction]);

  // Режим «Путь»: закреплённый путь подсвечен, пока его не снимут; закрепление живёт,
  // пока не пришёл другой ответ (другие концы или длина).
  const paths = data.state === "ready" ? data.paths : undefined;
  const [pin, setPin] = useState<{ paths: PathView[]; index: number } | null>(null);
  const pinnedIndex = pin !== null && pin.paths === paths ? pin.index : null;
  const pinnedIds = useMemo(
    () => (paths && pinnedIndex !== null ? pathNodes(paths[pinnedIndex]) : null),
    [paths, pinnedIndex],
  );
  // Подсветка закреплённого пути — и в новом рендерере (смена 2D↔3D); снятие — при откреплении.
  useEffect(() => {
    if (!api?.emphasize || pinnedIds === null) return;
    api.emphasize(pinnedIds);
    return () => api.emphasize?.(null);
  }, [api, pinnedIds]);
  const onPathHover = useCallback(
    (index: number | null) => {
      const hovered = index === null || !paths ? null : pathNodes(paths[index]);
      apiRef.current?.emphasize?.(hovered ?? pinnedIds);
    },
    [paths, pinnedIds],
  );
  // Открепление снимает подсветку (уборка эффекта выше).
  const onPathPin = useCallback(
    (index: number | null) => setPin(index === null || !paths ? null : { paths, index }),
    [paths],
  );

  // Легенда: наведение подсвечивает узлы пункта, клик — фильтр через URL (как чипы).
  const onLegendHover = useCallback(
    (target: LegendTarget | null) => {
      const current = apiRef.current;
      if (!current?.emphasize) return;
      if (!target || !graph) {
        // Ушли с легенды — снова виден закреплённый путь, если он есть.
        current.emphasize(pinnedIds);
        return;
      }
      const ids = new Set<string>();
      graph.forEachNode((id, a) => {
        if (target.kind === "type" ? a.type === target.value : a.community === target.value) ids.add(id);
      });
      current.emphasize(ids);
    },
    [graph, pinnedIds],
  );
  const onLegendToggle = useCallback(
    (target: LegendTarget) => {
      apiRef.current?.emphasize?.(null);
      if (target.kind === "type") {
        const types = search.types.includes(target.value)
          ? search.types.filter((t) => t !== target.value)
          : [...search.types, target.value];
        update({ types });
      } else if (mode !== "community") {
        // В режиме «Сообщество» сообщество выбрано в карточке режима, легенда его не снимает.
        update({ community: search.community === target.value ? null : target.value });
      }
    },
    [search.types, search.community, mode, update],
  );

  const downloadPng = async () => {
    if (!api || exporting) return;
    setExporting(true);
    setNotice("");
    try {
      const blob = await api.exportPng();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `mnogobase-graph-${search.view}.png`;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch {
      setNotice("Не удалось сохранить PNG. Попробуйте ещё раз, когда граф отрисуется.");
    } finally {
      setExporting(false);
    }
  };

  // Сообщение гаснет само.
  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(""), 4000);
    return () => clearTimeout(t);
  }, [notice]);

  // Путей не нашлось — объяснение в карточке режима, а не «под фильтры ничего не попало».
  const noPaths = paths !== undefined && paths.length === 0;
  const empty = visible !== null && visible.nodes.size === 0 && !noPaths;
  const truncated = data.state === "ready" && data.truncated;
  const showRotate = search.view === "3d" && !reducedMotion;
  const deselect = useCallback(() => setSelected(null), [setSelected]);
  const showCard = graph !== null && selected !== null && visible !== null && visible.nodes.has(selected);
  // Размер сообщества выбранной сущности вне обзора — с сервера: на графе режима видна лишь часть
  // участников. В окрестности — из списка крупнейших сообществ (тот же запрос, что у списка режима).
  const selectedCommunity = showCard ? graph.getNodeAttribute(selected, "community") : null;
  const topCommunities = useCommunities(
    COMMUNITIES_LIMIT,
    (mode === "neighbors" || mode === "path") && selectedCommunity !== null,
  );
  let communitySize: number | null | undefined;
  if (mode === "community") {
    communitySize =
      data.state === "ready" && selectedCommunity === search.community ? (data.communitySize ?? null) : null;
  } else if (mode !== "overview") {
    communitySize = topCommunities.data?.items.find((c) => c.community === selectedCommunity)?.size ?? null;
  }

  let canvas: ReactNode;
  if (data.state === "input") {
    canvas = (
      <CanvasMessage>
        <p className="max-w-xs font-serif text-lg font-semibold">{data.prompt}</p>
      </CanvasMessage>
    );
  } else if (data.state === "error" && notFound(data.error) && (mode === "neighbors" || mode === "path")) {
    canvas = (
      <CanvasMessage>
        <div className="max-w-sm space-y-3">
          <p className="font-serif text-lg font-semibold">Сущность не найдена: возможно, её удалили вместе с документом.</p>
          {mode === "neighbors" ? (
            <button type="button" onClick={() => update({ center: null, focus: null, expand: [] })} className={CANVAS_BUTTON}>
              Выбрать другую сущность
            </button>
          ) : (
            <button
              type="button"
              onClick={() => update({ source: null, target: null, focus: null, expand: [] })}
              className={CANVAS_BUTTON}
            >
              Выбрать другие сущности
            </button>
          )}
        </div>
      </CanvasMessage>
    );
  } else if (data.state === "error" && notFound(data.error) && mode === "community") {
    canvas = (
      <CanvasMessage>
        <div className="max-w-sm space-y-3">
          <p className="font-serif text-lg font-semibold">Сообщество не найдено: анализ графа мог устареть.</p>
          <button type="button" onClick={() => update({ community: null, focus: null, expand: [] })} className={CANVAS_BUTTON}>
            К списку сообществ
          </button>
        </div>
      </CanvasMessage>
    );
  } else if (data.state === "error") {
    canvas = (
      <CanvasMessage>
        <div className="max-w-sm space-y-2">
          <p className="font-serif text-xl font-semibold">Граф пока недоступен</p>
          <p className="text-sm text-muted">
            {data.error instanceof ApiError ? data.error.detail : data.error instanceof Error ? data.error.message : "Нет данных"}.
            {/* Без сессии совет не нужен: 401 уводит на страницу входа. */}
            {isUnauthorized(data.error) ? "" : " Обновите страницу чуть позже."}
          </p>
        </div>
      </CanvasMessage>
    );
  } else if (!graph) {
    canvas = (
      <CanvasMessage>
        <p className="flex items-center gap-2 text-sm text-muted" role="status">
          <span className="size-2 animate-pulse rounded-full bg-primary" aria-hidden="true" />
          Собираем граф…
        </p>
      </CanvasMessage>
    );
  } else if (search.view === "3d") {
    // Чанк 3D-вида мог исчезнуть после обновления сайта: ошибка — только в области графа.
    canvas = (
      <ErrorBoundary
        fallback={(error) => (
          <CanvasMessage>
            <div className="pointer-events-auto">
              <ErrorNotice error={error} />
            </div>
          </CanvasMessage>
        )}
      >
        <Suspense
          fallback={
            <CanvasMessage>
              <p className="flex items-center gap-2 text-sm text-muted" role="status">
                <span className="size-2 animate-pulse rounded-full bg-primary" aria-hidden="true" />
                Загружаем 3D-вид…
              </p>
            </CanvasMessage>
          }
        >
          <Graph3D
            graph={graph}
            filters={search}
            entityTypes={configured}
            typeLabel={catalog.typeLabel}
            selected={selected}
            onSelect={setSelected}
            onBackgroundClick={deselect}
            onReady={onReady3d}
            onExpand={live ? expandNode : undefined}
            autoRotate={showRotate && autoRotate}
          />
        </Suspense>
      </ErrorBoundary>
    );
  } else {
    canvas = (
      <Graph2D
        graph={graph}
        filters={search}
        entityTypes={configured}
        typeLabel={catalog.typeLabel}
        selected={selected}
        onSelect={setSelected}
        onBackgroundClick={deselect}
        onReady={onReady2d}
        onExpand={live ? expandNode : undefined}
      />
    );
  }

  return (
    <section className="paper-grid relative h-full overflow-hidden border-t border-line">
      <h1 className="sr-only">Граф</h1>
      <div ref={canvasRef} className="absolute inset-0">
        {canvas}
      </div>
      {empty && (
        <CanvasMessage>
          <div className={`${GLASS} pointer-events-auto max-w-sm space-y-3 px-6 py-5`}>
            <p className="font-serif text-lg font-semibold">Под фильтры ничего не попало</p>
            <p className="text-sm text-muted">Ослабьте ограничения или начните заново.</p>
            <button
              type="button"
              onClick={reset}
              className="rounded-xl bg-primary-deep px-4 py-2 text-sm text-on-primary shadow-card transition-opacity hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
            >
              Сбросить фильтры
            </button>
          </div>
        </CanvasMessage>
      )}

      <div
        ref={toolbarRef}
        className="pointer-events-none absolute inset-x-2.5 top-2.5 z-20 flex flex-wrap items-start justify-between gap-2 sm:inset-x-3 sm:top-3"
      >
        <div className="group/modes pointer-events-auto relative">
          <div role="group" aria-label="Режим графа" className={`${GLASS} flex p-1`}>
            {MODES.map((m) => {
              const pressed = m === mode;
              // На фикстурах есть только обзор: остальные режимы требуют живых данных.
              const disabled = !live && m !== "overview";
              return (
                <button
                  key={m}
                  type="button"
                  aria-pressed={pressed}
                  aria-disabled={disabled ? true : undefined}
                  aria-describedby={disabled ? modeHint : undefined}
                  onClick={(e) => {
                    if (disabled) e.preventDefault();
                    else if (!pressed) setMode(m);
                  }}
                  className={`h-8 rounded-xl px-2.5 text-[13px] transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary sm:px-3 ${
                    pressed
                      ? "bg-primary-deep font-medium text-on-primary"
                      : disabled
                        ? "cursor-not-allowed text-muted"
                        : "text-ink hover:bg-ink/[0.06]"
                  }`}
                >
                  {MODE_LABELS[m]}
                </button>
              );
            })}
          </div>
          {!live && (
            <p
              id={modeHint}
              role="tooltip"
              className="pointer-events-none absolute top-full left-0 z-10 mt-2 w-max max-w-64 rounded-lg bg-ink px-2.5 py-1.5 text-xs text-bg opacity-0 shadow-card transition-opacity group-has-[[aria-disabled=true]:focus-visible]/modes:opacity-100 group-has-[[aria-disabled=true]:hover]/modes:opacity-100"
            >
              Доступно на живых данных
            </p>
          )}
        </div>

        <div className="pointer-events-auto relative">
          <div className={`${GLASS} flex flex-wrap items-center gap-1 p-1`}>
            {column.available && (
              <button
                type="button"
                onClick={column.open}
                aria-label="Фильтры"
                title="Фильтры"
                className={`${TOOL} ${column.collapsed ? "" : "md:hidden"}`}
              >
                <SlidersHorizontal size={16} />
                <span className="hidden lg:inline">Фильтры</span>
              </button>
            )}
            <Segmented label="Вид" options={VIEW_OPTIONS} value={search.view} onChange={switchView} layoutId="graph-view" size="sm" />
            {showRotate && (
              <button
                type="button"
                role="switch"
                aria-checked={autoRotate}
                aria-label="Автоповорот"
                title="Автоповорот"
                onClick={() => setAutoRotate((v) => !v)}
                className={TOOL}
              >
                <RotateCw size={16} />
                <span className="hidden lg:inline">Автоповорот</span>
                <Toggle on={autoRotate} />
              </button>
            )}
            <span className="mx-0.5 hidden h-5 w-px bg-line sm:block" aria-hidden="true" />
            <button type="button" aria-label="Приблизить" title="Приблизить (+)" disabled={!api} onClick={() => api?.zoomIn?.()} className={ICON_TOOL}>
              <Plus size={16} />
            </button>
            <button type="button" aria-label="Отдалить" title="Отдалить (−)" disabled={!api} onClick={() => api?.zoomOut?.()} className={ICON_TOOL}>
              <Minus size={16} />
            </button>
            <button type="button" aria-label="Вписать" title="Вписать в экран (F)" disabled={!api} onClick={() => api?.fit()} className={TOOL}>
              <Maximize2 size={16} />
              <span className="hidden sm:inline">Вписать</span>
            </button>
            <button
              type="button"
              aria-label="PNG"
              title="Сохранить PNG"
              disabled={!api || exporting}
              onClick={() => void downloadPng()}
              className={TOOL}
            >
              <Download size={16} />
              <span className="hidden sm:inline">PNG</span>
            </button>
            <button
              type="button"
              aria-label="Горячие клавиши"
              title="Горячие клавиши (?)"
              aria-expanded={keysOpen}
              aria-controls={keysId}
              onClick={() => setKeysOpen((v) => !v)}
              className={`${ICON_TOOL} hidden sm:inline-flex ${keysOpen ? "bg-ink/[0.07]" : ""}`}
            >
              <Keyboard size={16} />
            </button>
          </div>
          {keysOpen && (
            <div
              id={keysId}
              role="region"
              aria-label="Горячие клавиши"
              className={`${GLASS} absolute top-full right-0 z-40 mt-2 w-64 bg-card/95 p-3`}
            >
              <dl className="space-y-1.5 text-[13px]">
                {KEYS.map(([k, label]) => (
                  <div key={k} className="flex items-center gap-3">
                    <dt className="flex w-12 shrink-0 justify-end gap-1">
                      {k.split("  ").map((part) => (
                        <kbd
                          key={part}
                          className="min-w-6 rounded-md border border-line bg-bg px-1.5 py-0.5 text-center font-sans text-xs font-medium text-ink"
                        >
                          {part}
                        </kbd>
                      ))}
                    </dt>
                    <dd className="text-muted">{label}</dd>
                  </div>
                ))}
              </dl>
            </div>
          )}
        </div>
      </div>

      {graph && (
        <div className="pointer-events-none absolute inset-x-2.5 bottom-2.5 z-10 flex items-end justify-between gap-2 sm:inset-x-3 sm:bottom-3">
          <Legend
            colorBy={search.colorBy}
            sizeBy={sizeMetric(graph, search.sizeBy)}
            catalog={catalog}
            types={legendTypes}
            activeTypes={search.types}
            activeCommunity={search.community}
            communities={communities}
            onToggle={onLegendToggle}
            onHover={onLegendHover}
          />
          {visible && (
            <p className={`${GLASS} hidden px-3 py-1.5 text-xs text-muted tabular-nums sm:block`} aria-live="polite">
              {formatInt(visible.nodes.size)} из {formatInt(graph.order)}{" "}
              {plural(graph.order, ["сущности", "сущностей", "сущностей"])}, {formatInt(visible.edges.size)}{" "}
              {plural(visible.edges.size, ["связь", "связи", "связей"])}
              {truncated && <span className="block text-right">Показаны не все связи</span>}
            </p>
          )}
        </div>
      )}

      <ModeCard
        mode={mode}
        search={search}
        update={update}
        compact={!wide}
        offsetTop={cardTop}
        needsInput={data.state === "input" || noPaths}
      >
        {paths && (
          <PathList
            paths={paths}
            timedOut={data.state === "ready" && data.timedOut === true}
            hops={search.hops}
            pinned={pinnedIndex}
            onPin={onPathPin}
            onHover={onPathHover}
            onWiden={() => update({ hops: 5 })}
          />
        )}
      </ModeCard>

      <AnimatePresence>
        {showCard && graph && selected && (
          <EntityCard
            key={wide ? "card" : "sheet"}
            offsetTop={cardTop}
            graph={graph}
            entityId={selected}
            catalog={catalog}
            communities={communities}
            rank={rank}
            communitySize={communitySize}
            compact={!wide}
            onSelect={setSelected}
            onShowCommunity={(community) =>
              live ? setMode("community", { community, focus: selected }) : update({ community })
            }
            shownCommunity={mode === "community" ? search.community : null}
            modesEnabled={live}
            onShowNeighbors={(id) => setMode("neighbors", { center: id, focus: id })}
            onPathFrom={(id) => setMode("path", { source: id, target: search.target === id ? null : search.target })}
            onExpand={expandNode}
            onClose={deselect}
          />
        )}
      </AnimatePresence>

      <p className="sr-only" role="status">
        {notice}
      </p>
      {notice && (
        <p className={`${GLASS} absolute top-16 left-1/2 z-40 max-w-xs -translate-x-1/2 px-3 py-2 text-xs`} aria-hidden="true">
          {notice}
        </p>
      )}
    </section>
  );
}

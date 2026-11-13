import { Download, Maximize2, RotateCw, SlidersHorizontal } from "lucide-react";
import { useReducedMotion } from "motion/react";
import { lazy, Suspense, useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { useEntityTypes, useOverview } from "../api/queries";
import { EntityPeek } from "../graph/EntityPeek";
import { Graph2D, type Graph2DApi } from "../graph/Graph2D";
import { communitySummaries, rankByPagerank } from "../graph/insights";
import { Legend } from "../graph/Legend";
import { buildGraph, sizeMetric, visibleSubgraph } from "../graph/model";
import type { GraphView } from "../graph/search";
import { useGraphSearch } from "../graph/useGraphSearch";
import { formatInt, plural } from "../lib/format";
import { useShellColumn } from "../shell/columnContext";
import { Sheet } from "../shell/Sheet";
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

const MODES = ["Обзор", "Окрестность", "Путь", "Сообщество"] as const;

const GLASS = "rounded-2xl border border-line bg-card/80 shadow-card backdrop-blur-md";
const TOOL =
  "inline-flex h-9 items-center gap-1.5 rounded-xl px-2.5 text-[13px] font-medium text-ink transition-colors hover:bg-ink/[0.06] focus-visible:outline-2 focus-visible:outline-primary disabled:cursor-not-allowed disabled:text-muted disabled:opacity-60 disabled:hover:bg-transparent";

function CanvasMessage({ children }: { children: ReactNode }) {
  return <div className="pointer-events-none absolute inset-0 grid place-items-center p-6 text-center">{children}</div>;
}

/** Обзор графа: холст 2D/3D, плавающая панель управления, легенда, карточка сущности. */
export function GraphPage() {
  const { search, update, reset } = useGraphSearch();
  const overview = useOverview();
  const entityTypes = useEntityTypes();
  const column = useShellColumn();
  const reducedMotion = useReducedMotion() ?? false;
  const modeHint = useId();

  const graph = useMemo(() => (overview.data ? buildGraph(overview.data.graph) : null), [overview.data]);
  const configured = useMemo(
    () => entityTypes.data ?? overview.data?.types.map((t) => t.type) ?? [],
    [entityTypes.data, overview.data],
  );
  const visible = useMemo(() => (graph ? visibleSubgraph(graph, search) : null), [graph, search]);
  const communities = useMemo(() => (graph ? communitySummaries(graph) : []), [graph]);
  const rank = useMemo(() => (graph ? rankByPagerank(graph) : new Map<string, number>()), [graph]);

  const [selected, setSelected] = useState<string | null>(null);
  const [autoRotate, setAutoRotate] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [notice, setNotice] = useState("");

  // Ручка рендерера помечена видом, которому принадлежит: после переключения 2D↔3D
  // старая ручка сразу перестаёт считаться текущей (3D без WebGL onReady не вызовет).
  const [renderer, setRenderer] = useState<{ view: GraphView; api: Graph2DApi } | null>(null);
  const api = renderer?.view === search.view ? renderer.api : null;
  const apiRef = useRef<Graph2DApi | null>(null);
  useEffect(() => {
    apiRef.current = api;
  }, [api]);

  const onReady2d = useCallback((next: Graph2DApi) => setRenderer({ view: "2d", api: next }), []);
  const onReady3d = useCallback((next: Graph2DApi) => setRenderer({ view: "3d", api: next }), []);

  const fitTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const scheduleFit = useCallback(() => {
    clearTimeout(fitTimer.current);
    fitTimer.current = setTimeout(() => apiRef.current?.fit(), FIT_DELAY_MS);
  }, []);
  useEffect(() => () => clearTimeout(fitTimer.current), []);

  // Sigma масштабирует кадр по всем узлам, включая скрытые: после смены состава вписываем видимые.
  const compositionKey = graph
    ? [search.types.join(","), search.community, search.minWeight, search.minMentions, search.hideIsolated].join("|")
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

  // Новый рендерер (смена вида) — вписать после первых кадров.
  useEffect(() => {
    if (api) scheduleFit();
  }, [api, scheduleFit]);

  const switchView = (view: GraphView) => {
    if (view === search.view) return;
    apiRef.current = null;
    update({ view });
  };

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

  const empty = visible !== null && visible.nodes.size === 0;
  const selectedName = selected && graph?.hasNode(selected) ? graph.getNodeAttribute(selected, "name") : "";
  const showRotate = search.view === "3d" && !reducedMotion;

  let canvas: ReactNode;
  if (overview.isError) {
    canvas = (
      <CanvasMessage>
        <div className="max-w-sm space-y-2">
          <p className="font-serif text-xl font-semibold">Граф пока недоступен</p>
          <p className="text-sm text-muted">
            {overview.error instanceof Error ? overview.error.message : "Нет данных"}. До этого интерфейс работает
            только на тестовых данных (сборка с VITE_FIXTURES=1).
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
    canvas = (
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
          selected={selected}
          onSelect={setSelected}
          onReady={onReady3d}
          autoRotate={showRotate && autoRotate}
        />
      </Suspense>
    );
  } else {
    canvas = (
      <Graph2D
        graph={graph}
        filters={search}
        entityTypes={configured}
        selected={selected}
        onSelect={setSelected}
        onReady={onReady2d}
      />
    );
  }

  return (
    <section className="h-full px-3 pb-3 md:px-8 md:pb-6">
      <h1 className="sr-only">Граф</h1>
      <div className="paper-grid relative h-full overflow-hidden rounded-[14px] border border-line shadow-card">
        <div className="absolute inset-0">{canvas}</div>
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

        <div className="pointer-events-none absolute inset-x-2.5 top-2.5 flex flex-wrap items-start justify-between gap-2 sm:inset-x-3 sm:top-3">
          <div className="group/modes pointer-events-auto relative">
            <div role="group" aria-label="Режим графа" className={`${GLASS} flex p-1`}>
              {MODES.map((mode, i) =>
                i === 0 ? (
                  <button
                    key={mode}
                    type="button"
                    aria-pressed="true"
                    className="h-8 rounded-xl bg-primary-deep px-3 text-[13px] font-medium text-on-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
                  >
                    {mode}
                  </button>
                ) : (
                  <button
                    key={mode}
                    type="button"
                    aria-pressed="false"
                    aria-disabled="true"
                    aria-describedby={modeHint}
                    onClick={(e) => e.preventDefault()}
                    className="h-8 cursor-not-allowed rounded-xl px-2.5 text-[13px] text-muted/75 focus-visible:outline-2 focus-visible:outline-primary sm:px-3"
                  >
                    {mode}
                  </button>
                ),
              )}
            </div>
            <p
              id={modeHint}
              role="tooltip"
              className="pointer-events-none absolute top-full left-0 z-10 mt-2 w-max max-w-64 rounded-lg bg-ink px-2.5 py-1.5 text-xs text-bg opacity-0 shadow-card transition-opacity group-has-[[aria-disabled=true]:focus-visible]/modes:opacity-100 group-has-[[aria-disabled=true]:hover]/modes:opacity-100"
            >
              Окрестность, путь и сообщество появятся после этапа 2
            </p>
          </div>

          <div className={`${GLASS} pointer-events-auto flex flex-wrap items-center gap-1 p-1`}>
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
            <button type="button" aria-label="Вписать" title="Вписать в экран" disabled={!api} onClick={() => api?.fit()} className={TOOL}>
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
          </div>
        </div>

        {graph && (
          <div className="pointer-events-none absolute inset-x-2.5 bottom-2.5 flex items-end justify-between gap-2 sm:inset-x-3 sm:bottom-3">
            <Legend
              colorBy={search.colorBy}
              sizeBy={sizeMetric(graph, search.sizeBy)}
              types={configured}
              activeTypes={search.types}
              communities={communities}
            />
            {visible && (
              <p className={`${GLASS} hidden px-3 py-1.5 text-xs text-muted tabular-nums sm:block`} aria-live="polite">
                {formatInt(visible.nodes.size)} из {formatInt(graph.order)}{" "}
                {plural(graph.order, ["сущности", "сущностей", "сущностей"])}, {formatInt(visible.edges.size)}{" "}
                {plural(visible.edges.size, ["связь", "связи", "связей"])}
              </p>
            )}
          </div>
        )}


        <p className="sr-only" role="status">
          {notice}
        </p>
        {notice && (
          <p className={`${GLASS} absolute top-16 right-3 max-w-xs px-3 py-2 text-xs`} aria-hidden="true">
            {notice}
          </p>
        )}
      </div>

      {graph && (
        <Sheet open={selected !== null} onClose={() => setSelected(null)} title={selectedName || "Сущность"}>
          {selected && (
            <EntityPeek
              graph={graph}
              entityId={selected}
              entityTypes={configured}
              communities={communities}
              rank={rank}
              onSelect={setSelected}
              onShowCommunity={(community) => update({ community })}
            />
          )}
        </Sheet>
      )}
    </section>
  );
}

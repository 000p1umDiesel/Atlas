import { MultiDirectedGraph } from "graphology";
import type { Attributes } from "graphology-types";
import { useCallback, useEffect, useRef, useState } from "react";
import Sigma from "sigma";
import type { NodeView } from "../api/types";
import { communityColorVar, cssColor, typeColorVar, withAlpha } from "../lib/entityColors";
import {
  applyFrame,
  encodeLayoutRequest,
  LayoutController,
  type LayoutFrame,
  type WorkerLike,
} from "./layoutProtocol";
import {
  DEFAULT_FILTERS,
  nodeSize,
  sizeMetric,
  sizeRange,
  visibleSubgraph,
  type Graph,
  type GraphFilters,
} from "./model";

export interface Graph2DApi {
  /** Снимок текущего вида в PNG (фон — `--bg`). */
  exportPng(): Promise<Blob>;
  /** Вписать видимые узлы в экран. */
  fit(): void;
  /** Освободить WebGL и воркер (повторный вызов безопасен; размонтирование делает то же). */
  dispose(): void;
}

export interface Graph2DProps {
  graph: Graph;
  filters: GraphFilters;
  /** Настроенные типы сущностей в порядке конфигурации (цвета как в легенде). */
  entityTypes?: string[];
  /** Выбранная сущность: её подпись видна всегда. */
  selected?: string | null;
  onSelect(entityId: string): void;
  onReady?(api: Graph2DApi): void;
}

type RenderNode = NodeView & { x: number; y: number; size: number; color: string; label: string };
type RenderEdge = { weight: number; predicate: string; size: number };
type RenderGraph = MultiDirectedGraph<RenderNode, RenderEdge>;
type NodeState = { isActive: boolean; isMatch: boolean; isSelected: boolean };
type EdgeState = { isActive: boolean };
type GraphState = { hasActive: boolean };
type Renderer = Sigma<RenderNode, RenderEdge, Attributes, NodeState, EdgeState, GraphState>;

/** Порог подписи: узлы мельче (в пикселях на экране) подписываются только при наведении. */
const LABEL_THRESHOLD = 9;
const MATCH_SCALE = 1.35;
const HOVER_SCALE = 1.15;
const EXPORT_TIMEOUT_MS = 3000;

interface Palette {
  label: string;
  edge: string;
  edgeActive: string;
  dim: string;
  edgeDim: string;
  ring: string;
  card: string;
  bg: string;
}

function readPalette(): Palette {
  const muted = cssColor("--muted");
  return {
    label: cssColor("--ink"),
    edge: withAlpha(muted, 0.45),
    edgeActive: cssColor("--primary"),
    dim: withAlpha(muted, 0.25),
    edgeDim: withAlpha(muted, 0.12),
    ring: cssColor("--primary-deep"),
    card: cssColor("--card"),
    bg: cssColor("--bg"),
  };
}

function prefersReducedMotion(): boolean {
  return typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/**
 * Граф для отрисовки: копия модели с позициями, размером, цветом и подписью.
 * Начальная раскладка — спираль золотого угла (разные точки, детерминированно).
 */
function toRenderGraph(graph: Graph): RenderGraph {
  const rg: RenderGraph = new MultiDirectedGraph();
  let i = 0;
  graph.forEachNode((id, a) => {
    const r = 10 * Math.sqrt(i + 1);
    const angle = i * 2.399963;
    rg.addNode(id, {
      ...a,
      x: r * Math.cos(angle),
      y: r * Math.sin(angle),
      size: 8,
      color: "rgb(201, 195, 182)",
      label: a.name,
    });
    i++;
  });
  let maxWeight = 0;
  graph.forEachEdge((_, a) => {
    if (Number.isFinite(a.weight)) maxWeight = Math.max(maxWeight, a.weight);
  });
  graph.forEachEdge((key, a, src, dst) => {
    const w = Number.isFinite(a.weight) && maxWeight > 0 ? Math.max(0, a.weight) / maxWeight : 0;
    rg.addEdgeWithKey(key, src, dst, {
      weight: a.weight,
      predicate: a.predicate,
      size: 0.6 + 1.6 * Math.sqrt(w),
    });
  });
  return rg;
}

function createLayoutWorker(): WorkerLike {
  return new Worker(new URL("./layout.worker.ts", import.meta.url), {
    type: "module",
  }) as unknown as WorkerLike;
}

/** Интерактивный 2D-граф (Sigma 4, WebGL) с живой раскладкой ForceAtlas2 в модульном воркере. */
export function Graph2D({ graph, filters, entityTypes, selected = null, onSelect, onReady }: Graph2DProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const sigmaRef = useRef<Renderer | null>(null);
  const renderGraphRef = useRef<RenderGraph | null>(null);
  const controllerRef = useRef<LayoutController | null>(null);
  const paletteRef = useRef<Palette | null>(null);
  const visibleRef = useRef<{ nodes: Set<string>; edges: Set<string> }>({
    nodes: new Set(),
    edges: new Set(),
  });
  const hoveredRef = useRef<string | null>(null);
  const onSelectRef = useRef(onSelect);
  const onReadyRef = useRef(onReady);
  const [themeVersion, setThemeVersion] = useState(0);

  useEffect(() => {
    onSelectRef.current = onSelect;
    onReadyRef.current = onReady;
  });

  // Активное подмножество при наведении: узел, его видимые соседи и рёбра к ним.
  const activeRef = useRef<{ nodes: string[]; edges: string[] }>({ nodes: [], edges: [] });
  const setHover = useCallback((node: string | null) => {
    const sigma = sigmaRef.current;
    const rg = renderGraphRef.current;
    if (!sigma || !rg) return;
    hoveredRef.current = node;
    const prev = activeRef.current;
    sigma.setNodesState(prev.nodes, { isActive: false });
    sigma.setEdgesState(prev.edges, { isActive: false });
    const visible = visibleRef.current;
    if (node === null || !rg.hasNode(node) || !visible.nodes.has(node)) {
      activeRef.current = { nodes: [], edges: [] };
      sigma.setGraphState({ hasActive: false });
      return;
    }
    const nodes = new Set([node]);
    const edges: string[] = [];
    rg.forEachEdge(node, (edge, _a, src, dst) => {
      if (!visible.edges.has(edge)) return;
      edges.push(edge);
      nodes.add(src === node ? dst : src);
    });
    activeRef.current = { nodes: [...nodes], edges };
    sigma.setNodesState(activeRef.current.nodes, { isActive: true });
    sigma.setEdgesState(edges, { isActive: true });
    sigma.setGraphState({ hasActive: true });
  }, []);

  // Создание рендерера и воркера на каждый новый граф; разрушение при размонтировании.
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const rg = toRenderGraph(graph);
    renderGraphRef.current = rg;
    paletteRef.current = readPalette();
    const pal = () => paletteRef.current as Palette;
    const reduced = prefersReducedMotion();

    const sigma: Renderer = new Sigma<RenderNode, RenderEdge, Attributes, NodeState, EdgeState, GraphState>(
      rg,
      container,
      {
        customNodeState: { isActive: false, isMatch: false, isSelected: false },
        customEdgeState: { isActive: false },
        customGraphState: { hasActive: false },
        settings: {
          // "shared" вставляет <style> (подсказка жестов) — CSP страницы его запрещает.
          gestureTarget: "graph",
          allowInvalidContainer: true,
          itemSizesReference: "screen",
          enableNodeDrag: true,
          enableCameraRotation: false,
          labelRenderedSizeThreshold: LABEL_THRESHOLD,
          labelDensity: 0.8,
          labelGridCellSize: 120,
          minCameraRatio: 0.03,
          maxCameraRatio: 20,
          ...(reduced
            ? { zoomDuration: 0, inertiaDuration: 0, doubleClickZoomingDuration: 0 }
            : {}),
        },
        styles: {
          nodes: [
            {
              x: { attribute: "x" },
              y: { attribute: "y" },
              size: { attribute: "size" },
              color: { attribute: "color" },
              label: { attribute: "label" },
              labelColor: () => pal().label,
              labelSize: 12,
              depth: "nodes",
              visibility: { whenState: "isHidden", then: "hidden", else: "visible" },
              labelVisibility: "auto",
              backdropVisibility: "hidden",
              cursor: "pointer",
            },
            {
              whenState: "isMatch",
              then: {
                size: (a) => a.size * MATCH_SCALE,
                labelVisibility: "visible",
                backdropVisibility: "visible",
                backdropArea: "node",
                backdropColor: "transparent",
                backdropShadowColor: "transparent",
                backdropShadowBlur: 0,
                backdropBorderColor: () => pal().ring,
                backdropBorderWidth: 2,
                backdropPadding: 2,
              },
            },
            {
              when: (_a, s, gs) => gs.hasActive && !s.isActive,
              then: {
                color: () => pal().dim,
                labelVisibility: "hidden",
                backdropVisibility: "hidden",
              },
            },
            { whenState: "isActive", then: { depth: "topNodes" } },
            {
              whenState: "isSelected",
              then: {
                labelVisibility: "visible",
                depth: "topNodes",
                backdropVisibility: "visible",
                backdropArea: "node",
                backdropColor: "transparent",
                backdropShadowColor: "transparent",
                backdropShadowBlur: 0,
                backdropBorderColor: () => pal().ring,
                backdropBorderWidth: 3,
                backdropPadding: 2,
              },
            },
            {
              whenState: "isHovered",
              then: {
                size: (a, s) => a.size * (s.isMatch ? MATCH_SCALE : 1) * HOVER_SCALE,
                labelVisibility: "visible",
                depth: "topNodes",
                backdropVisibility: "visible",
                backdropArea: "both",
                backdropColor: () => pal().card,
                backdropShadowColor: "rgba(0, 0, 0, 0.18)",
                backdropShadowBlur: 8,
                backdropBorderWidth: 0,
                backdropPadding: 4,
              },
            },
          ],
          edges: [
            {
              size: { attribute: "size" },
              color: () => pal().edge,
              depth: "edges",
              visibility: { whenState: "isHidden", then: "hidden", else: "visible" },
            },
            {
              when: (_a, s, gs) => gs.hasActive && !s.isActive,
              then: { color: () => pal().edgeDim },
            },
            { whenState: "isActive", then: { color: () => pal().edgeActive, depth: "topEdges" } },
          ],
        },
      },
    );
    sigmaRef.current = sigma;

    sigma.on("enterNode", ({ node }) => setHover(node));
    sigma.on("leaveNode", () => setHover(null));
    sigma.on("clickNode", ({ node }) => onSelectRef.current(node));
    // Перетаскивание (встроенное в Sigma 4: камера не двигается, отпускание — mouseup):
    // раскладка останавливается, чтобы не тянуть узел обратно.
    sigma.on("nodeDragStart", () => controllerRef.current?.stop());

    const controller = new LayoutController(createLayoutWorker, (ids, frame: LayoutFrame) => {
      // При prefers-reduced-motion узлы не «плывут»: применяется только итоговый кадр.
      if (reduced && !frame.done) return;
      applyFrame(rg, ids, frame);
    });
    controllerRef.current = controller;

    // Смена темы: data-theme на <html> или системная схема.
    const bump = () => setThemeVersion((v) => v + 1);
    const observer = new MutationObserver(bump);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    const scheme = typeof matchMedia === "function" ? matchMedia("(prefers-color-scheme: dark)") : null;
    scheme?.addEventListener("change", bump);
    // Sigma сама слушает только window.resize; панели вокруг меняют размер контейнера.
    const resizer =
      typeof ResizeObserver === "function" ? new ResizeObserver(() => sigma.scheduleRender()) : null;
    resizer?.observe(container);

    let disposed = false;
    const dispose = () => {
      if (disposed) return;
      disposed = true;
      observer.disconnect();
      scheme?.removeEventListener("change", bump);
      resizer?.disconnect();
      controller.dispose();
      sigma.kill();
      if (sigmaRef.current === sigma) sigmaRef.current = null;
      if (controllerRef.current === controller) controllerRef.current = null;
      if (renderGraphRef.current === rg) renderGraphRef.current = null;
      activeRef.current = { nodes: [], edges: [] };
      hoveredRef.current = null;
    };

    const fit = () => {
      if (disposed) return;
      const camera = sigma.getCamera();
      const duration = prefersReducedMotion() ? 0 : 300;
      let minX = Infinity;
      let maxX = -Infinity;
      let minY = Infinity;
      let maxY = -Infinity;
      for (const id of visibleRef.current.nodes) {
        if (!rg.hasNode(id)) continue;
        const p = sigma.graphToFramedGraph(rg.getNodeAttributes(id));
        minX = Math.min(minX, p.x);
        maxX = Math.max(maxX, p.x);
        minY = Math.min(minY, p.y);
        maxY = Math.max(maxY, p.y);
      }
      if (!(minX <= maxX)) {
        void camera.reset({ duration });
        return;
      }
      const ratio = Math.min(1, Math.max(0.05, Math.max(maxX - minX, maxY - minY) * 1.15));
      void camera.animate({ x: (minX + maxX) / 2, y: (minY + maxY) / 2, ratio, angle: 0 }, { duration });
    };

    const exportPng = () =>
      new Promise<Blob>((resolve, reject) => {
        if (disposed) {
          reject(new Error("Граф уже закрыт"));
          return;
        }
        const timer = setTimeout(() => reject(new Error("Граф не отрисовался")), EXPORT_TIMEOUT_MS);
        // WebGL-буфер (preserveDrawingBuffer: false) читается в том же кадре, что и отрисовка.
        sigma.once("afterRender", () => {
          clearTimeout(timer);
          const stage = sigma.getStageCanvas();
          const out = document.createElement("canvas");
          out.width = stage.width;
          out.height = stage.height;
          const ctx = out.getContext("2d");
          if (!ctx) {
            reject(new Error("Canvas недоступен"));
            return;
          }
          ctx.fillStyle = pal().bg;
          ctx.fillRect(0, 0, out.width, out.height);
          ctx.drawImage(stage, 0, 0);
          out.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("Не удалось получить PNG"))), "image/png");
        });
        sigma.scheduleRender();
      });

    onReadyRef.current?.({ exportPng, fit, dispose });
    return dispose;
  }, [graph, setHover]);

  // Видимость по фильтрам и перезапуск раскладки — только при смене состава графа.
  const typesKey = filters.types.join("\u0000");
  const { community, minWeight, minMentions, hideIsolated } = filters;
  useEffect(() => {
    const sigma = sigmaRef.current;
    const rg = renderGraphRef.current;
    if (!sigma || !rg) return;
    const visible = visibleSubgraph(graph, {
      ...DEFAULT_FILTERS,
      types: typesKey === "" ? [] : typesKey.split("\u0000"),
      community,
      minWeight,
      minMentions,
      hideIsolated,
    });
    visibleRef.current = visible;
    const shown: string[] = [];
    const hidden: string[] = [];
    rg.forEachNode((id) => (visible.nodes.has(id) ? shown : hidden).push(id));
    sigma.setNodesState(shown, { isHidden: false });
    sigma.setNodesState(hidden, { isHidden: true });
    const shownEdges: string[] = [];
    const hiddenEdges: string[] = [];
    rg.forEachEdge((key) => (visible.edges.has(key) ? shownEdges : hiddenEdges).push(key));
    sigma.setEdgesState(shownEdges, { isHidden: false });
    sigma.setEdgesState(hiddenEdges, { isHidden: true });
    setHover(hoveredRef.current);
    controllerRef.current?.start(encodeLayoutRequest(rg, visible));
    // Цвет, размер и подсветка в зависимости не входят: они не перезапускают раскладку.
  }, [graph, setHover, typesKey, community, minWeight, minMentions, hideIsolated]);

  // Подсветка по строке поиска (подстрока имени без учёта регистра), только среди видимых.
  const highlight = filters.highlight.trim().toLowerCase();
  useEffect(() => {
    const sigma = sigmaRef.current;
    const rg = renderGraphRef.current;
    if (!sigma || !rg) return;
    const on: string[] = [];
    const off: string[] = [];
    const visible = visibleRef.current.nodes;
    rg.forEachNode((id, a) => {
      const match = highlight !== "" && visible.has(id) && a.name.toLowerCase().includes(highlight);
      (match ? on : off).push(id);
    });
    sigma.setNodesState(off, { isMatch: false });
    sigma.setNodesState(on, { isMatch: true });
  }, [graph, highlight, typesKey, community, minWeight, minMentions, hideIsolated]);

  // Цвет и размер узлов; при смене темы цвета пересчитываются заново.
  const { colorBy, sizeBy } = filters;
  const typesOrder = (entityTypes ?? []).join("\u0000");
  useEffect(() => {
    const sigma = sigmaRef.current;
    const rg = renderGraphRef.current;
    if (!sigma || !rg) return;
    paletteRef.current = readPalette();
    const configured =
      typesOrder !== ""
        ? typesOrder.split("\u0000")
        : [...new Set(graph.mapNodes((_, a) => a.type))].sort();
    const metric = sizeMetric(graph, sizeBy);
    const range = sizeRange(graph, sizeBy);
    rg.updateEachNodeAttributes(
      (_, a) => ({
        ...a,
        size: nodeSize(a, metric, range),
        color: cssColor(colorBy === "community" ? communityColorVar(a.community) : typeColorVar(a.type, configured)),
      }),
      { attributes: ["size", "color"] },
    );
    sigma.refresh();
  }, [graph, colorBy, sizeBy, typesOrder, themeVersion]);

  // Выбранный узел: подпись всегда видна, кольцо вокруг.
  useEffect(() => {
    const sigma = sigmaRef.current;
    const rg = renderGraphRef.current;
    if (!sigma || !rg || selected === null || !rg.hasNode(selected)) return;
    sigma.setNodeState(selected, { isSelected: true });
    return () => {
      if (sigmaRef.current === sigma && rg.hasNode(selected)) sigma.setNodeState(selected, { isSelected: false });
    };
  }, [graph, selected]);

  return (
    <div
      ref={containerRef}
      className="relative h-full min-h-0 w-full overflow-hidden"
      role="img"
      aria-label="Граф связей между сущностями"
    />
  );
}

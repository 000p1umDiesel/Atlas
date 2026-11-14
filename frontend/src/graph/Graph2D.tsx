import { MultiDirectedGraph } from "graphology";
import type { Attributes } from "graphology-types";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import Sigma from "sigma";
import type { EdgeExtremitySpec, NodeLayerSpec } from "sigma/primitives";
import { extremityArrow, layerFill } from "sigma/rendering";
import type { NodeView } from "../api/types";
import { cssColor, isDarkScheme, mixRgb, schemeColor, typeColorRgb, withAlpha } from "../lib/entityColors";
import { fontsReady, graphFont, GRAPH_FONT_FAMILY, loadFonts } from "../lib/fonts";
import { GraphTooltip, placeTooltip, type TooltipInfo } from "./GraphTooltip";
import { communityColorMap, predicateLabel } from "./insights";
import {
  fitRatio,
  framingRatio,
  neighborhood,
  NO_INSETS,
  smoothTowards,
  usableInsets,
  type FitInsets,
  type Point,
} from "./interaction";
import {
  applyFrame,
  encodeLayoutRequest,
  LAYOUT_ITERATIONS,
  LayoutController,
  LayoutDragGuard,
  type LayoutFrame,
  type WorkerLike,
} from "./layoutProtocol";
import {
  DEFAULT_FILTERS,
  nodeSize,
  sizeBounds,
  sizeMetric,
  sizeRange,
  visibleSubgraph,
  type Graph,
  type GraphFilters,
} from "./model";
import { seedPositions, type SeedPosition } from "./seed";

export type { FitInsets } from "./interaction";

/** Сдвиг точки фокуса от центра холста, CSS-пиксели (место под карточку сущности). */
export interface FocusOffset {
  x: number;
  y: number;
}

export interface Graph2DApi {
  /** Снимок текущего вида в PNG. */
  exportPng(): Promise<Blob>;
  /**
   * Вписать видимые узлы в экран; `insets` — места под плавающие панели по краям холста
   * (CSS-пиксели): узлы окажутся в свободном прямоугольнике между ними.
   */
  fit(insets?: FitInsets): void;
  /** Освободить WebGL и воркер (повторный вызов безопасен; размонтирование делает то же). */
  dispose(): void;
  /** Приблизить / отдалить на шаг. */
  zoomIn?(): void;
  zoomOut?(): void;
  /**
   * Навести камеру на узел: он окажется в центре, сдвинутом на `offset`, в кадре — его соседи.
   * Пока раскладка двигает узел (новый рендерер после смены вида), камера следует за ним.
   */
  focusNode?(id: string, offset?: FocusOffset): void;
  /** Подсветить набор узлов (наведение на пункт легенды); null — снять. */
  emphasize?(ids: ReadonlySet<string> | null): void;
}

export interface Graph2DProps {
  graph: Graph;
  filters: GraphFilters;
  /** Настроенные типы сущностей в порядке конфигурации (цвета как в легенде). */
  entityTypes?: string[];
  /** Русская подпись типа для подсказки у курсора (без неё — ключ типа). */
  typeLabel?(type: string): string;
  /** Выбранная сущность: её окрестность подсвечена, подписи видны. */
  selected?: string | null;
  onSelect(entityId: string): void;
  /** Клик по пустому месту холста (снять выделение, закрыть карточку). */
  onBackgroundClick?(): void;
  onReady?(api: Graph2DApi): void;
  /** Двойной клик по узлу: добавить его соседей на граф (камера при этом не приближается). */
  onExpand?(entityId: string): void;
  /** Колесо мыши масштабирует граф (по умолчанию). false — прокручивает страницу (мини-граф). */
  wheelZoom?: boolean;
}

type RenderNode = NodeView & { x: number; y: number; size: number; color: string; label: string };
type RenderEdge = { weight: number; predicate: string; size: number };
type RenderGraph = MultiDirectedGraph<RenderNode, RenderEdge>;
/** isActive — не приглушён; isNeighbor — окрестность наведения/выбора (подпись видна всегда). */
type NodeState = { isActive: boolean; isNeighbor: boolean; isMatch: boolean; isSelected: boolean };
type EdgeState = { isActive: boolean };
type GraphState = { hasActive: boolean };
/** Свои переменные шейдера узла: цвет обводки (слой `ring`). */
type Primitives = {
  nodes: { variables: { ringColor: { type: "color"; default: string } }; layers: NodeLayerSpec[] };
  edges: {
    extremities: EdgeExtremitySpec[];
    label: { minVisibilityThreshold: number; fullVisibilityThreshold: number };
  };
};
type Renderer = Sigma<RenderNode, RenderEdge, Attributes, NodeState, EdgeState, GraphState, Primitives>;

/** Порог подписи: узлы мельче (в пикселях на экране) подписываются только при наведении. */
const LABEL_THRESHOLD = 9;
const LABEL_SIZE = 13;
const EDGE_LABEL_SIZE = 11;
const MATCH_SCALE = 1.35;
const HOVER_SCALE = 1.15;
const EXPORT_TIMEOUT_MS = 3000;
/** Постоянная времени следования узла за курсором при перетаскивании, мс. */
const DRAG_TAU_MS = 90;
const FOCUS_MS = 450;
/** Постоянная времени, с которой камера догоняет выбранный узел, пока его двигает раскладка, мс. */
const FOLLOW_TAU_MS = 140;
/** Запас у края свободной части кадра при наведении на окрестность (подписи соседей), px. */
const FRAME_PAD = 36;
const ZOOM_MS = 260;

const FONT_REGULAR = `500 ${GRAPH_FONT_FAMILY}`;
const FONT_STRONG = `600 ${GRAPH_FONT_FAMILY}`;
const FONTS = [graphFont(500, LABEL_SIZE), graphFont(600, LABEL_SIZE)];

/** GL_UNSIGNED_BYTE: константа, а не `WebGL2RenderingContext.…` (в jsdom его нет). */
const UNSIGNED_BYTE = 5121;

/**
 * Тонкая обводка узла цветом холста (слой фрагментного шейдера Sigma 4): край узла
 * читается чисто, а налезающие друг на друга узлы разделены просветом.
 */
const ringLayer = {
  name: "ring",
  uniforms: [],
  attributes: [{ name: "ringColor", size: 4 as const, type: UNSIGNED_BYTE, normalized: true, source: "ringColor" }],
  glsl: /* glsl */ `
vec4 layer_ring(vec4 v_ringColor) {
  float w = 1.4 * context.pixelToUV;
  float t = smoothstep(-w - context.aaWidth, -w + context.aaWidth, context.sdf);
  return vec4(v_ringColor.rgb, v_ringColor.a * t);
}
`,
};

interface Palette {
  ink: string;
  muted: string;
  edge: string;
  edgeActive: string;
  edgeDim: string;
  card: string;
  labelBg: string;
}

function readPalette(): Palette {
  const ink = cssColor("--ink");
  const card = cssColor("--card");
  const dark = isDarkScheme();
  return {
    ink,
    muted: cssColor("--muted"),
    edge: withAlpha(ink, dark ? 0.24 : 0.26),
    edgeActive: dark ? cssColor("--primary-ink") : cssColor("--primary-deep"),
    edgeDim: withAlpha(ink, 0.06),
    card,
    labelBg: withAlpha(card, 0.82),
  };
}

function prefersReducedMotion(): boolean {
  return typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/**
 * Граф для отрисовки: копия модели с позициями, размером, цветом и подписью. Узлы из
 * `seeds` (прежний граф, `seedPositions`) стартуют со своих мест, остальные — по спирали
 * золотого угла (разные точки, детерминированно).
 */
function toRenderGraph(graph: Graph, seeds: ReadonlyMap<string, SeedPosition> = new Map()): RenderGraph {
  const rg: RenderGraph = new MultiDirectedGraph();
  let i = 0;
  graph.forEachNode((id, a) => {
    const seed = seeds.get(id);
    const r = 10 * Math.sqrt(i + 1);
    const angle = i * 2.399963;
    rg.addNode(id, {
      ...a,
      x: seed ? seed.x : r * Math.cos(angle),
      y: seed ? seed.y : r * Math.sin(angle),
      size: 8,
      color: "rgb(189, 182, 168)",
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
      size: 0.8 + 1.8 * Math.sqrt(w),
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
export function Graph2D({
  graph,
  filters,
  entityTypes,
  typeLabel,
  selected = null,
  onSelect,
  onBackgroundClick,
  onReady,
  onExpand,
  wheelZoom = true,
}: Graph2DProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const tooltipRef = useRef<HTMLDivElement>(null);
  const pointerRef = useRef({ x: 0, y: 0 });
  const sigmaRef = useRef<Renderer | null>(null);
  const renderGraphRef = useRef<RenderGraph | null>(null);
  const controllerRef = useRef<LayoutController | null>(null);
  const dragGuardRef = useRef<LayoutDragGuard | null>(null);
  const paletteRef = useRef<Palette | null>(null);
  const visibleRef = useRef<{ nodes: Set<string>; edges: Set<string> }>({
    nodes: new Set(),
    edges: new Set(),
  });
  const hoveredRef = useRef<string | null>(null);
  const selectedRef = useRef<string | null>(selected);
  const emphasisRef = useRef<ReadonlySet<string> | null>(null);
  const onSelectRef = useRef(onSelect);
  const onBackgroundRef = useRef(onBackgroundClick);
  const onReadyRef = useRef(onReady);
  const onExpandRef = useRef(onExpand);
  const wheelZoomRef = useRef(wheelZoom);
  const typeLabelRef = useRef(typeLabel);
  /** Позиции узлов прежнего рендерера: новый граф (раскрытие, смена режима) стартует с них. */
  const positionsRef = useRef<ReadonlyMap<string, SeedPosition>>(new Map());
  const [themeVersion, setThemeVersion] = useState(0);
  // Sigma создаётся после загрузки шрифта подписей: атлас глифов рисуется один раз.
  const [fontsLoaded, setFontsLoaded] = useState(() => fontsReady(FONTS));
  const [tooltip, setTooltip] = useState<TooltipInfo | null>(null);

  useEffect(() => {
    onSelectRef.current = onSelect;
    onBackgroundRef.current = onBackgroundClick;
    onReadyRef.current = onReady;
    onExpandRef.current = onExpand;
    wheelZoomRef.current = wheelZoom;
    typeLabelRef.current = typeLabel;
  });

  useEffect(() => {
    if (fontsLoaded) return;
    let cancelled = false;
    void loadFonts(FONTS).then(() => {
      if (!cancelled) setFontsLoaded(true);
    });
    return () => {
      cancelled = true;
    };
  }, [fontsLoaded]);

  // Активное подмножество: окрестность наведённого узла, иначе узлы пункта легенды,
  // иначе окрестность выбранного. Всё остальное приглушено.
  const activeRef = useRef<{ nodes: string[]; edges: string[]; neighbors: string[] }>({
    nodes: [],
    edges: [],
    neighbors: [],
  });
  const refreshActive = useCallback(() => {
    const sigma = sigmaRef.current;
    const rg = renderGraphRef.current;
    if (!sigma || !rg) return;
    const prev = activeRef.current;
    sigma.setNodesState(prev.nodes, { isActive: false, isNeighbor: false });
    sigma.setEdgesState(prev.edges, { isActive: false });
    const visible = visibleRef.current;
    const hovered = hoveredRef.current;
    const emphasis = emphasisRef.current;
    let next = { nodes: [] as string[], edges: [] as string[], neighbors: [] as string[] };
    if (hovered !== null && visible.nodes.has(hovered)) {
      const n = neighborhood(rg, hovered, visible);
      next = { ...n, neighbors: n.nodes };
    } else if (emphasis !== null) {
      next = { nodes: [...emphasis].filter((id) => visible.nodes.has(id)), edges: [], neighbors: [] };
    } else if (selectedRef.current !== null) {
      const n = neighborhood(rg, selectedRef.current, visible);
      next = { ...n, neighbors: n.nodes };
    }
    activeRef.current = next;
    sigma.setNodesState(next.nodes, { isActive: true });
    sigma.setNodesState(next.neighbors, { isNeighbor: true });
    sigma.setEdgesState(next.edges, { isActive: true });
    sigma.setGraphState({ hasActive: next.nodes.length > 0 || emphasis !== null });
  }, []);

  const showTooltip = useCallback((node: string | null) => {
    const rg = renderGraphRef.current;
    if (node === null || !rg || !rg.hasNode(node)) {
      setTooltip(null);
      return;
    }
    const a = rg.getNodeAttributes(node);
    const type = typeLabelRef.current?.(a.type) ?? a.type;
    setTooltip({ id: node, name: a.name, type, mentions: a.mention_count, color: a.color });
  }, []);

  // Создание рендерера и воркера на каждый новый граф; разрушение при размонтировании.
  useEffect(() => {
    const container = containerRef.current;
    if (!container || !fontsLoaded) return;
    const rg = toRenderGraph(graph, seedPositions(graph, positionsRef.current));
    renderGraphRef.current = rg;
    const remember = () => {
      positionsRef.current = new Map(rg.mapNodes((id, a) => [id, { x: a.x, y: a.y }]));
    };
    paletteRef.current = readPalette();
    const pal = () => paletteRef.current as Palette;
    const reduced = prefersReducedMotion();

    // Перетаскивание: Sigma сообщает цель (позицию указателя), узел догоняет её плавно.
    let drag: { node: string; target: { x: number; y: number }; last: number; released: boolean } | null = null;
    let dragFrame = 0;
    const dragStep = (now: number) => {
      dragFrame = 0;
      const d = drag;
      if (!d || !rg.hasNode(d.node)) {
        drag = null;
        return;
      }
      const cur = rg.getNodeAttributes(d.node);
      const next = smoothTowards(cur, d.target, Math.min(64, now - d.last), DRAG_TAU_MS);
      d.last = now;
      rg.mergeNodeAttributes(d.node, next);
      dragGuardRef.current?.follow(d.node, next.x, next.y);
      if (d.released && Math.hypot(d.target.x - next.x, d.target.y - next.y) < 1e-3) {
        // Догнал: ровно в точку отпускания, и раскладка держит его там же.
        rg.mergeNodeAttributes(d.node, d.target);
        dragGuardRef.current?.settle(d.node, d.target.x, d.target.y);
        drag = null;
        return;
      }
      dragFrame = requestAnimationFrame(dragStep);
    };
    const follow = (node: string, x: number, y: number) => {
      if (!drag || drag.node !== node) drag = { node, target: { x, y }, last: performance.now(), released: false };
      else drag.target = { x, y };
      if (!dragFrame) dragFrame = requestAnimationFrame(dragStep);
    };

    const sigma: Renderer = new Sigma<RenderNode, RenderEdge, Attributes, NodeState, EdgeState, GraphState, Primitives>(
      rg,
      container,
      {
        customNodeState: { isActive: false, isNeighbor: false, isMatch: false, isSelected: false },
        customEdgeState: { isActive: false },
        customGraphState: { hasActive: false },
        primitives: {
          nodes: {
            variables: { ringColor: { type: "color", default: "#fffdf8" } },
            layers: [layerFill(), ringLayer],
          },
          edges: {
            extremities: [extremityArrow({ lengthRatio: 5, widthRatio: 4 })],
            // Подпись связи — только целиком: если не помещается вдоль ребра, её нет (не обрубок).
            label: { minVisibilityThreshold: 0.98, fullVisibilityThreshold: 1 },
          },
        },
        settings: {
          // "shared" вставляет <style> (подсказка жестов) — CSP страницы его запрещает.
          gestureTarget: "graph",
          allowInvalidContainer: true,
          itemSizesReference: "screen",
          enableNodeDrag: true,
          enableCameraRotation: false,
          renderEdgeLabels: true,
          edgeLabelAnchors: "allNodes",
          minEdgeThickness: 0.8,
          labelRenderedSizeThreshold: LABEL_THRESHOLD,
          labelDensity: 0.7,
          labelGridCellSize: 140,
          minCameraRatio: 0.03,
          maxCameraRatio: 20,
          // Мягкая инерция камеры после панорамирования (по умолчанию 200 мс — резко).
          inertiaDuration: 700,
          inertiaRatio: 4,
          zoomDuration: 320,
          dragPositionToAttributes: (pos, node) => {
            if (reduced) return { x: pos.x, y: pos.y };
            follow(node, pos.x, pos.y);
            return {};
          },
          ...(reduced ? { zoomDuration: 0, inertiaDuration: 0, doubleClickZoomingDuration: 0 } : {}),
        },
        styles: {
          nodes: [
            {
              x: { attribute: "x" },
              y: { attribute: "y" },
              size: { attribute: "size" },
              color: { attribute: "color" },
              ringColor: () => pal().card,
              label: { attribute: "label" },
              labelColor: () => pal().ink,
              labelSize: LABEL_SIZE,
              labelFont: FONT_REGULAR,
              labelBackgroundColor: () => pal().labelBg,
              labelBackgroundPadding: 2,
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
                labelFont: FONT_STRONG,
                backdropVisibility: "visible",
                backdropArea: "node",
                backdropColor: "transparent",
                backdropShadowColor: "transparent",
                backdropShadowBlur: 0,
                backdropBorderColor: () => pal().edgeActive,
                backdropBorderWidth: 2,
                backdropPadding: 2,
              },
            },
            {
              when: (_a, s, gs) => gs.hasActive && !s.isActive,
              then: {
                color: (a) => mixRgb(a.color, pal().card, 0.8),
                labelVisibility: "hidden",
                backdropVisibility: "hidden",
              },
            },
            { whenState: "isActive", then: { depth: "topNodes" } },
            { whenState: "isNeighbor", then: { labelVisibility: "visible" } },
            {
              whenState: "isSelected",
              then: {
                labelVisibility: "visible",
                labelFont: FONT_STRONG,
                depth: "topNodes",
                backdropVisibility: "visible",
                backdropArea: "node",
                backdropColor: "transparent",
                backdropShadowColor: "transparent",
                backdropShadowBlur: 0,
                backdropBorderColor: () => pal().edgeActive,
                backdropBorderWidth: 3,
                backdropPadding: 2.5,
              },
            },
            {
              whenState: "isHovered",
              then: {
                size: (a, s) => a.size * (s.isMatch ? MATCH_SCALE : 1) * HOVER_SCALE,
                labelVisibility: "visible",
                labelFont: FONT_STRONG,
                depth: "topNodes",
              },
            },
          ],
          edges: [
            {
              size: { attribute: "size" },
              color: () => pal().edge,
              depth: "edges",
              visibility: { whenState: "isHidden", then: "hidden", else: "visible" },
              head: "none",
              label: (a) => predicateLabel(a.predicate),
              labelVisibility: "hidden",
              labelSize: EDGE_LABEL_SIZE,
              labelFont: FONT_REGULAR,
              labelColor: () => pal().muted,
              labelBackgroundColor: () => pal().labelBg,
              labelBackgroundPadding: 2,
              labelPosition: "over",
            },
            {
              when: (_a, s, gs) => gs.hasActive && !s.isActive,
              then: { color: () => pal().edgeDim },
            },
            {
              whenState: "isActive",
              then: {
                color: () => pal().edgeActive,
                depth: "topEdges",
                head: "arrow",
                size: (a) => Math.max(1.8, a.size),
                labelVisibility: "visible",
              },
            },
          ],
        },
      },
    );
    sigmaRef.current = sigma;

    // Касание — не наведение: у пальца нет «ухода» с узла, а после перелёта камеры Sigma
    // заново «наводит» на то, что оказалось под местом последнего касания.
    let touch = false;
    const onPointerType = (e: PointerEvent) => {
      touch = e.pointerType === "touch";
    };
    container.addEventListener("pointerdown", onPointerType, true);
    sigma.on("enterNode", ({ node, event }) => {
      if (touch) return;
      hoveredRef.current = node;
      const original = event.original;
      if ("clientX" in original) pointerRef.current = { x: original.clientX, y: original.clientY };
      refreshActive();
      showTooltip(node);
    });
    sigma.on("leaveNode", () => {
      hoveredRef.current = null;
      refreshActive();
      showTooltip(null);
    });
    sigma.on("clickNode", ({ node }) => onSelectRef.current(node));
    sigma.on("clickStage", () => onBackgroundRef.current?.());
    // Двойной клик по узлу: первый клик уже выбрал его, второй — раскрывает соседей.
    // Зум камеры по двойному клику (поведение Sigma) на узле не нужен.
    sigma.on("doubleClickNode", (e) => {
      e.preventSigmaDefault();
      onExpandRef.current?.(e.node);
    });
    // Без масштаба колесом (мини-граф на странице): событие уходит странице, она прокручивается.
    const passWheel = (e: { preventSigmaDefault(): void }) => {
      if (!wheelZoomRef.current) e.preventSigmaDefault();
    };
    sigma.on("wheelStage", passWheel);
    sigma.on("wheelNode", passWheel);
    sigma.on("wheelEdge", passWheel);

    // Кадр камеры на узел: соседи 1 шага видны в свободной части холста (без карточки).
    const focusState = (id: string, offset: FocusOffset) => {
      const p = sigma.graphToFramedGraph(rg.getNodeAttributes(id));
      const { width, height } = sigma.getDimensions();
      const probe = { cameraState: { x: p.x, y: p.y, ratio: 1, angle: 0 } };
      const center = sigma.framedGraphToViewport(p, probe);
      const offsets: Point[] = [];
      for (const n of neighborhood(rg, id, visibleRef.current).nodes) {
        if (n === id) continue;
        const q = sigma.framedGraphToViewport(sigma.graphToFramedGraph(rg.getNodeAttributes(n)), probe);
        offsets.push({ x: q.x - center.x, y: q.y - center.y });
      }
      const ratio = framingRatio(offsets, {
        x: width / 2 - Math.abs(offset.x) - FRAME_PAD,
        y: height / 2 - Math.abs(offset.y) - FRAME_PAD,
      });
      // Камера в узле, затем сдвиг на offset: узел окажется левее/выше центра на offset.
      const shifted = sigma.viewportToFramedGraph(
        { x: width / 2 + offset.x, y: height / 2 + offset.y },
        { cameraState: { x: p.x, y: p.y, ratio, angle: 0 } },
      );
      return { x: shifted.x, y: shifted.y, ratio, angle: 0 };
    };

    // Слежение за выбранным узлом, пока раскладка его двигает: новый рендерер после смены
    // вида стартует с исходной спирали, и узел уезжает из кадра. Снимается, когда раскладка
    // сошлась (финальный перелёт), при «Вписать», масштабе и любом жесте пользователя.
    let tracking: { id: string; offset: FocusOffset } | null = null;
    let layoutRunning = true;
    let framesApplied = 0;
    let lastFollow = 0;
    const trackFocus = (done: boolean) => {
      const f = tracking;
      if (!f) return;
      if (!rg.hasNode(f.id) || !visibleRef.current.nodes.has(f.id)) {
        tracking = null;
        return;
      }
      const camera = sigma.getCamera();
      const target = focusState(f.id, f.offset);
      if (done) {
        tracking = null;
        void camera.animate(target, { duration: reduced ? 0 : FOCUS_MS });
        return;
      }
      const now = performance.now();
      const k = 1 - Math.exp(-Math.min(64, now - lastFollow) / FOLLOW_TAU_MS);
      lastFollow = now;
      const cur = camera.getState();
      camera.setState({
        x: cur.x + (target.x - cur.x) * k,
        y: cur.y + (target.y - cur.y) * k,
        ratio: cur.ratio * Math.pow(target.ratio / cur.ratio, k),
        angle: 0,
      });
    };
    const stopFollow = () => {
      tracking = null;
    };
    container.addEventListener("wheel", stopFollow, { capture: true, passive: true });
    container.addEventListener("pointerdown", stopFollow, true);

    const controller = new LayoutController(createLayoutWorker, (ids, frame: LayoutFrame) => {
      layoutRunning = frame.done !== true;
      // При prefers-reduced-motion узлы не «плывут»: применяется только итоговый кадр.
      if (reduced && !frame.done) return;
      applyFrame(rg, ids, frame, drag ? new Set([drag.node]) : undefined);
      framesApplied++;
      remember();
      trackFocus(frame.done === true);
    });
    controllerRef.current = controller;
    // Перетаскивание встроено в Sigma 4 (камера не двигается, отпускание — pointerup).
    // Живой режим: раскладка продолжается, перетаскиваемый узел закреплён у курсора и
    // тянет соседей. При prefers-reduced-motion — остановка и перезапуск после отпускания.
    const dragGuard = new LayoutDragGuard(
      controller,
      (pinned) => encodeLayoutRequest(rg, visibleRef.current, LAYOUT_ITERATIONS, pinned),
      { live: !reduced },
    );
    dragGuardRef.current = dragGuard;
    sigma.on("nodeDragStart", ({ node, event }) => {
      dragGuard.start(event.x, event.y, node);
      showTooltip(null);
    });
    sigma.on("nodeDrag", ({ event }) => dragGuard.move(event.x, event.y));
    sigma.on("nodeDragEnd", ({ allDraggedNodes }) => {
      // Воркер сразу получает точку отпускания: узел ещё догоняет её, но назад его не отбросит.
      dragGuard.end(allDraggedNodes, drag ? { [drag.node]: drag.target } : {});
      if (drag) drag.released = true;
    });

    const onPointerMove = (e: PointerEvent) => {
      touch = e.pointerType === "touch";
      pointerRef.current = { x: e.clientX, y: e.clientY };
      if (hoveredRef.current !== null) placeTooltip(tooltipRef.current, container, e.clientX, e.clientY);
    };
    container.addEventListener("pointermove", onPointerMove);

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
      remember();
      cancelAnimationFrame(dragFrame);
      drag = null;
      container.removeEventListener("pointermove", onPointerMove);
      container.removeEventListener("pointerdown", onPointerType, true);
      container.removeEventListener("wheel", stopFollow, true);
      container.removeEventListener("pointerdown", stopFollow, true);
      tracking = null;
      observer.disconnect();
      scheme?.removeEventListener("change", bump);
      resizer?.disconnect();
      controller.dispose();
      sigma.kill();
      if (sigmaRef.current === sigma) sigmaRef.current = null;
      if (controllerRef.current === controller) controllerRef.current = null;
      if (dragGuardRef.current === dragGuard) dragGuardRef.current = null;
      if (renderGraphRef.current === rg) renderGraphRef.current = null;
      activeRef.current = { nodes: [], edges: [], neighbors: [] };
      hoveredRef.current = null;
      setTooltip(null);
    };

    // «Вписать»: видимые узлы — в свободной части холста (без панелей по краям, `insets`).
    const fit = (insets: FitInsets = NO_INSETS) => {
      if (disposed) return;
      tracking = null;
      const camera = sigma.getCamera();
      const duration = prefersReducedMotion() ? 0 : 300;
      let minX = Infinity;
      let maxX = -Infinity;
      let minY = Infinity;
      let maxY = -Infinity;
      const framed: Point[] = [];
      for (const id of visibleRef.current.nodes) {
        if (!rg.hasNode(id)) continue;
        const p = sigma.graphToFramedGraph(rg.getNodeAttributes(id));
        framed.push(p);
        minX = Math.min(minX, p.x);
        maxX = Math.max(maxX, p.x);
        minY = Math.min(minY, p.y);
        maxY = Math.max(maxY, p.y);
      }
      if (!(minX <= maxX)) {
        void camera.reset({ duration });
        return;
      }
      const { width, height } = sigma.getDimensions();
      const pad = usableInsets(insets, width, height, FRAME_PAD);
      // Размах в пикселях экрана при ratio = 1 (как в focusState): по нему — нужный масштаб.
      const c = { x: (minX + maxX) / 2, y: (minY + maxY) / 2 };
      const probe = { cameraState: { x: c.x, y: c.y, ratio: 1, angle: 0 } };
      let left = Infinity;
      let right = -Infinity;
      let top = Infinity;
      let bottom = -Infinity;
      for (const p of framed) {
        const q = sigma.framedGraphToViewport(p, probe);
        left = Math.min(left, q.x);
        right = Math.max(right, q.x);
        top = Math.min(top, q.y);
        bottom = Math.max(bottom, q.y);
      }
      const ratio = fitRatio(
        { x: right - left, y: bottom - top },
        { x: width - pad.left - pad.right - 2 * FRAME_PAD, y: height - pad.top - pad.bottom - 2 * FRAME_PAD },
      );
      // Центр узлов — в центр свободного прямоугольника: камера сдвинута на половину разницы отступов.
      const shifted = sigma.viewportToFramedGraph(
        { x: width / 2 - (pad.left - pad.right) / 2, y: height / 2 - (pad.top - pad.bottom) / 2 },
        { cameraState: { x: c.x, y: c.y, ratio, angle: 0 } },
      );
      void camera.animate({ x: shifted.x, y: shifted.y, ratio, angle: 0 }, { duration });
    };

    const zoom = (dir: 1 | -1) => {
      if (disposed) return;
      tracking = null;
      const camera = sigma.getCamera();
      const duration = prefersReducedMotion() ? 0 : ZOOM_MS;
      void (dir > 0 ? camera.zoomIn({ duration }) : camera.zoomOut({ duration }));
    };

    const focusNode = (id: string, offset: FocusOffset = { x: 0, y: 0 }) => {
      // Неизвестный или скрытый узел — камера не трогается (кадр «весь граф» остаётся).
      if (disposed || !rg.hasNode(id) || !visibleRef.current.nodes.has(id)) return;
      const camera = sigma.getCamera();
      camera.cancelAnimation();
      if (layoutRunning) {
        // Узел ещё движется: камера ведёт его до конца раскладки. Свежий рендерер (кадров
        // раскладки ещё не было) сразу ставит камеру на узел — без кадра «весь граф».
        tracking = { id, offset };
        lastFollow = performance.now();
        if (framesApplied === 0) camera.setState(focusState(id, offset));
        return;
      }
      tracking = null;
      void camera.animate(focusState(id, offset), { duration: prefersReducedMotion() ? 0 : FOCUS_MS });
    };

    const emphasize = (ids: ReadonlySet<string> | null) => {
      if (disposed) return;
      emphasisRef.current = ids;
      refreshActive();
    };

    const exportPng = () =>
      new Promise<Blob>((resolve, reject) => {
        if (disposed) {
          reject(new Error("Граф уже закрыт"));
          return;
        }
        // WebGL-буфер (preserveDrawingBuffer: false) читается в том же кадре, что и отрисовка.
        const capture = () => {
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
          ctx.fillStyle = pal().card;
          ctx.fillRect(0, 0, out.width, out.height);
          ctx.drawImage(stage, 0, 0);
          out.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("Не удалось получить PNG"))), "image/png");
        };
        const timer = setTimeout(() => {
          sigma.off("afterRender", capture);
          reject(new Error("Граф не отрисовался"));
        }, EXPORT_TIMEOUT_MS);
        sigma.once("afterRender", capture);
        sigma.scheduleRender();
      });

    onReadyRef.current?.({
      exportPng,
      fit,
      dispose,
      zoomIn: () => zoom(1),
      zoomOut: () => zoom(-1),
      focusNode,
      emphasize,
    });
    return dispose;
  }, [graph, fontsLoaded, refreshActive, showTooltip]);

  // Видимость по фильтрам и перезапуск раскладки — только при смене состава графа.
  const typesKey = filters.types.join("\u0000");
  const predicatesKey = filters.predicates.join("\u0000");
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
      predicates: predicatesKey === "" ? [] : predicatesKey.split("\u0000"),
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
    refreshActive();
    controllerRef.current?.start(
      encodeLayoutRequest(rg, visible, LAYOUT_ITERATIONS, dragGuardRef.current?.pinned),
    );
    // Цвет, размер и подсветка в зависимости не входят: они не перезапускают раскладку.
  }, [graph, fontsLoaded, refreshActive, typesKey, predicatesKey, community, minWeight, minMentions, hideIsolated]);

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
  }, [graph, fontsLoaded, highlight, typesKey, predicatesKey, community, minWeight, minMentions, hideIsolated]);

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
    const communities = colorBy === "community" ? communityColorMap(graph) : null;
    const metric = sizeMetric(graph, sizeBy);
    const range = sizeRange(graph, sizeBy);
    const bounds = sizeBounds(graph);
    // Без подсказки `{ attributes: [...] }`: с ней Sigma 4 пропускает переиндексацию и
    // ищет узлы в своём индексе программ, а он пуст, пока ждёт полная переобработка
    // (`window.resize` → `scheduleRefresh()` до следующего кадра, потерянный WebGL-контекст).
    // Тогда Sigma бросает «node … can't be repaint». Полный refresh ниже всё равно
    // переиндексирует граф, так что подсказка ничего не экономила.
    rg.updateEachNodeAttributes((_, a) => ({
      ...a,
      size: nodeSize(a, metric, range, bounds),
      color: communities
        ? schemeColor(a.community === null ? undefined : communities.get(a.community))
        : typeColorRgb(a.type, configured),
    }));
    sigma.refresh();
  }, [graph, fontsLoaded, colorBy, sizeBy, typesOrder, themeVersion]);

  // Выбранный узел: кольцо, подпись, подсвеченная окрестность.
  useEffect(() => {
    selectedRef.current = selected;
    const sigma = sigmaRef.current;
    const rg = renderGraphRef.current;
    if (!sigma || !rg) return;
    refreshActive();
    if (selected === null || !rg.hasNode(selected)) return;
    sigma.setNodeState(selected, { isSelected: true });
    return () => {
      if (sigmaRef.current === sigma && rg.hasNode(selected)) sigma.setNodeState(selected, { isSelected: false });
    };
  }, [graph, fontsLoaded, selected, refreshActive]);

  // Подсказка появилась — сразу к курсору, без кадра в углу.
  useLayoutEffect(() => {
    if (tooltip) placeTooltip(tooltipRef.current, containerRef.current, pointerRef.current.x, pointerRef.current.y);
  }, [tooltip]);

  return (
    <div
      ref={containerRef}
      className="relative h-full min-h-0 w-full overflow-hidden"
      role="img"
      aria-label="Граф связей между сущностями"
    >
      {tooltip && <GraphTooltip ref={tooltipRef} info={tooltip} />}
    </div>
  );
}

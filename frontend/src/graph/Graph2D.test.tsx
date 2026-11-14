import { act, render } from "@testing-library/react";
import type { AbstractGraph } from "graphology-types";
import { generateFixtures } from "../fixtures/generate";
import { Graph2D, type Graph2DApi } from "./Graph2D";
import { buildGraph, DEFAULT_FILTERS, visibleSubgraph, type GraphFilters } from "./model";
import { mergeParts } from "./dataset";

/**
 * Подмена Sigma 4.0.6 в той части, где живёт ошибка «node can't be repaint».
 * Настоящая Sigma на `eachNodeAttributesUpdated` с подсказкой `attributes`, в которой нет
 * x/y/zIndex/type, пропускает переиндексацию и берёт место узла из `nodeProgramIndex`.
 * Этот индекс пуст, пока ждёт полная переобработка: `scheduleRefresh()` после
 * `window.resize` или `setSetting`, а также после потери WebGL-контекста (render выходит
 * сразу). Тогда Sigma бросает исключение прямо из обработчика события graphology.
 */
type Handler = (payload: unknown) => void;
type CameraState = { x: number; y: number; ratio: number; angle: number };
const sigmaState = vi.hoisted(() => ({
  instances: [] as Array<{
    indexed: boolean;
    killed: boolean;
    graph: AbstractGraph;
    handlers: Map<string, Handler[]>;
    camState: CameraState;
    dims: { width: number; height: number };
    framedGraphToViewport(p: { x: number; y: number }, o?: { cameraState?: CameraState }): { x: number; y: number };
    graphToFramedGraph(p: { x: number; y: number }): { x: number; y: number };
  }>,
}));

vi.mock("sigma", () => {
  const LAYOUT = new Set(["x", "y", "zIndex", "type"]);
  class FakeSigma {
    indexed = false;
    killed = false;
    readonly handlers = new Map<string, Array<(payload: unknown) => void>>();
    private readonly listener: (e: { hints?: { attributes?: Array<string | number> } }) => void;
    constructor(readonly graph: AbstractGraph) {
      this.listener = (e) => {
        const fields = e.hints?.attributes;
        const layoutChanged = !fields || fields.some((f) => LAYOUT.has(String(f)));
        if (!layoutChanged && !this.indexed) {
          throw new Error(`Sigma: node "${graph.nodes()[0]}" can't be repaint`);
        }
      };
      graph.on("eachNodeAttributesUpdated", this.listener);
      this.refresh();
      sigmaState.instances.push(this);
    }
    /** Аналог `window.resize` → `scheduleRefresh()`: индексы очищены до следующего кадра. */
    schedulePendingFullRefresh() {
      this.indexed = false;
    }
    refresh() {
      this.indexed = true;
      return this;
    }
    kill() {
      this.killed = true;
      this.indexed = false;
      this.graph.removeListener("eachNodeAttributesUpdated", this.listener);
    }
    on(name: string, fn: (payload: unknown) => void) {
      this.handlers.set(name, [...(this.handlers.get(name) ?? []), fn]);
      return this;
    }
    once() {
      return this;
    }
    off() {
      return this;
    }
    /**
     * Камера и координаты, как у Sigma: граф нормирован в квадрат [0, 1] (весь граф при
     * ratio = 1 занимает меньшую сторону холста), экран — от камеры, ось y вверх.
     */
    dims = { width: 1000, height: 600 };
    camState = { x: 0.5, y: 0.5, ratio: 1, angle: 0 };
    readonly camera = {
      getState: () => ({ ...this.camState }),
      setState: (s: Partial<typeof this.camState>) => {
        this.camState = { ...this.camState, ...s };
      },
      animate: async (s: Partial<typeof this.camState>) => {
        this.camState = { ...this.camState, ...s };
      },
      reset: async () => {
        this.camState = { x: 0.5, y: 0.5, ratio: 1, angle: 0 };
      },
      cancelAnimation: () => {},
      zoomIn: async () => {},
      zoomOut: async () => {},
    };
    getCamera() {
      return this.camera;
    }
    getDimensions() {
      return this.dims;
    }
    graphToFramedGraph(p: { x: number; y: number }) {
      let minX = Infinity;
      let minY = Infinity;
      let maxX = -Infinity;
      let maxY = -Infinity;
      this.graph.forEachNode((_, a) => {
        minX = Math.min(minX, a.x as number);
        maxX = Math.max(maxX, a.x as number);
        minY = Math.min(minY, a.y as number);
        maxY = Math.max(maxY, a.y as number);
      });
      const span = Math.max(maxX - minX, maxY - minY) || 1;
      return { x: (p.x - minX) / span, y: (p.y - minY) / span };
    }
    framedGraphToViewport(p: { x: number; y: number }, o?: { cameraState?: { x: number; y: number; ratio: number } }) {
      const c = o?.cameraState ?? this.camState;
      const k = Math.min(this.dims.width, this.dims.height);
      return {
        x: this.dims.width / 2 + ((p.x - c.x) * k) / c.ratio,
        y: this.dims.height / 2 - ((p.y - c.y) * k) / c.ratio,
      };
    }
    viewportToFramedGraph(v: { x: number; y: number }, o?: { cameraState?: { x: number; y: number; ratio: number } }) {
      const c = o?.cameraState ?? this.camState;
      const k = Math.min(this.dims.width, this.dims.height);
      return {
        x: c.x + ((v.x - this.dims.width / 2) * c.ratio) / k,
        y: c.y - ((v.y - this.dims.height / 2) * c.ratio) / k,
      };
    }
    setNodeState() {}
    setNodesState() {}
    setEdgesState() {}
    setGraphState() {}
    scheduleRender() {}
  }
  return { default: FakeSigma };
});

// Фабрики слоёв обращаются к WebGL2RenderingContext, которого в jsdom нет.
vi.mock("sigma/rendering", () => ({ layerFill: () => ({}), extremityArrow: () => ({}) }));

class FakeWorker {
  onmessage = null;
  onerror = null;
  postMessage() {}
  terminate() {}
}

beforeEach(() => {
  sigmaState.instances.length = 0;
  vi.stubGlobal("Worker", FakeWorker);
  // В jsdom нет canvas: цвета из CSS-переменных разбираются без него.
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(() => null);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const graph = buildGraph(generateFixtures().graph);
const filters = (over: Partial<GraphFilters> = {}): GraphFilters => ({ ...DEFAULT_FILTERS, ...over });

test("смена размера и цвета не падает, пока Sigma ждёт полной переобработки", () => {
  const { rerender } = render(<Graph2D graph={graph} filters={filters()} onSelect={() => {}} />);
  const sigma = sigmaState.instances.at(-1) as unknown as { schedulePendingFullRefresh(): void };
  expect(sigma).toBeDefined();

  // window.resize в том же кадре, что и клик по «Размер по: Упоминания».
  sigma.schedulePendingFullRefresh();
  expect(() =>
    rerender(<Graph2D graph={graph} filters={filters({ sizeBy: "mentions" })} onSelect={() => {}} />),
  ).not.toThrow();

  sigma.schedulePendingFullRefresh();
  expect(() =>
    rerender(
      <Graph2D graph={graph} filters={filters({ sizeBy: "mentions", colorBy: "community" })} onSelect={() => {}} />,
    ),
  ).not.toThrow();
});

test("смена темы не падает, пока Sigma ждёт полной переобработки", async () => {
  render(<Graph2D graph={graph} filters={filters()} onSelect={() => {}} />);
  const sigma = sigmaState.instances.at(-1) as unknown as { schedulePendingFullRefresh(): void };
  sigma.schedulePendingFullRefresh();
  // MutationObserver на data-theme → пересчёт цветов.
  await act(async () => {
    document.documentElement.setAttribute("data-theme", "dark");
    await new Promise((r) => setTimeout(r, 0));
  });
  document.documentElement.removeAttribute("data-theme");
});

/** Событие Sigma с `preventSigmaDefault`, как у настоящей (обработчики страницы зовут его). */
function emit(name: string, payload: Record<string, unknown> = {}) {
  const sigma = sigmaState.instances.at(-1)!;
  const preventSigmaDefault = vi.fn();
  for (const fn of sigma.handlers.get(name) ?? []) fn({ ...payload, preventSigmaDefault, event: { preventSigmaDefault } });
  return preventSigmaDefault;
}

test("wheelZoom={false}: колесо мыши не масштабирует холст (страница прокручивается)", () => {
  const { unmount } = render(<Graph2D graph={graph} filters={filters()} onSelect={() => {}} wheelZoom={false} />);
  expect(emit("wheelStage")).toHaveBeenCalled();
  expect(emit("wheelNode", { node: graph.nodes()[0] })).toHaveBeenCalled();
  unmount();
  render(<Graph2D graph={graph} filters={filters()} onSelect={() => {}} />);
  expect(emit("wheelStage")).not.toHaveBeenCalled();
});

test("двойной клик по узлу — onExpand без зума камеры", () => {
  const onExpand = vi.fn();
  render(<Graph2D graph={graph} filters={filters()} onSelect={() => {}} onExpand={onExpand} />);
  const id = graph.nodes()[3]!;
  const prevented = emit("doubleClickNode", { node: id });
  expect(onExpand).toHaveBeenCalledWith(id);
  expect(prevented).toHaveBeenCalled();
});

test("новый граф: прежние узлы остаются на местах, новый сосед — рядом с размещённым", () => {
  const { graph: part } = generateFixtures();
  const small = buildGraph({ nodes: part.nodes.slice(0, 30), relations: part.relations });
  const anchor = small.nodes()[0]!;
  const bigger = buildGraph(
    mergeParts([
      { nodes: part.nodes.slice(0, 30), relations: part.relations },
      {
        nodes: [{ ...part.nodes[0]!, entity_id: "fresh", name: "Новый" }],
        relations: [{ ...part.relations[0]!, src_id: anchor, dst_id: "fresh", predicate: "knows" }],
      },
    ]),
  );
  const { rerender } = render(<Graph2D graph={small} filters={filters()} onSelect={() => {}} />);
  const before = sigmaState.instances.at(-1)!.graph;
  const positions = new Map(before.mapNodes((id, a) => [id, { x: a.x as number, y: a.y as number }]));
  rerender(<Graph2D graph={bigger} filters={filters()} onSelect={() => {}} />);
  const after = sigmaState.instances.at(-1)!.graph;
  expect(after).not.toBe(before);
  for (const [id, p] of positions) {
    expect(after.getNodeAttribute(id, "x")).toBeCloseTo(p.x, 6);
    expect(after.getNodeAttribute(id, "y")).toBeCloseTo(p.y, 6);
  }
  const a = positions.get(anchor)!;
  const fresh = after.getNodeAttributes("fresh") as { x: number; y: number };
  expect(Math.hypot(fresh.x - a.x, fresh.y - a.y)).toBeCloseTo(12, 6);
});

test("focusNode на скрытый фильтрами или неизвестный узел ничего не делает (камера не трогается)", () => {
  let api: Graph2DApi | null = null;
  const person = graph.findNode((_, a) => a.type === "Person")!;
  const other = graph.findNode((_, a) => a.type !== "Person")!;
  render(<Graph2D graph={graph} filters={filters({ types: ["Person"] })} onSelect={() => {}} onReady={(a) => (api = a)} />);
  expect(api).not.toBeNull();
  const sigma = sigmaState.instances.at(-1)!;
  const before = { ...sigma.camState };
  api!.focusNode?.(other);
  api!.focusNode?.("no-such-node");
  expect(sigma.camState).toEqual(before);
  api!.focusNode?.(person);
  expect(sigma.camState).not.toEqual(before);
});

/** Экранные точки видимых узлов при текущей камере подменной Sigma. */
function screenPoints(f: GraphFilters) {
  const sigma = sigmaState.instances.at(-1)!;
  return [...visibleSubgraph(graph, f).nodes].map((id) =>
    sigma.framedGraphToViewport(sigma.graphToFramedGraph(sigma.graph.getNodeAttributes(id) as { x: number; y: number })),
  );
}

test("fit с отступами: все видимые узлы — в свободном прямоугольнике, не под панелями", () => {
  let api: Graph2DApi | null = null;
  render(<Graph2D graph={graph} filters={filters()} onSelect={() => {}} onReady={(a) => (api = a)} />);
  const insets = { top: 60, right: 0, bottom: 90, left: 292 };
  api!.fit(insets);
  const { width, height } = sigmaState.instances.at(-1)!.dims;
  for (const p of screenPoints(filters())) {
    expect(p.x).toBeGreaterThanOrEqual(insets.left);
    expect(p.x).toBeLessThanOrEqual(width - insets.right);
    expect(p.y).toBeGreaterThanOrEqual(insets.top);
    expect(p.y).toBeLessThanOrEqual(height - insets.bottom);
  }
});

test("fit с карточкой справа: узлы левее карточки; без отступов — весь холст", () => {
  let api: Graph2DApi | null = null;
  render(<Graph2D graph={graph} filters={filters()} onSelect={() => {}} onReady={(a) => (api = a)} />);
  const { width, height } = sigmaState.instances.at(-1)!.dims;
  api!.fit({ top: 0, right: 332, bottom: 0, left: 0 });
  for (const p of screenPoints(filters())) expect(p.x).toBeLessThanOrEqual(width - 332);
  api!.fit();
  const points = screenPoints(filters());
  for (const p of points) {
    expect(p.x).toBeGreaterThanOrEqual(0);
    expect(p.x).toBeLessThanOrEqual(width);
    expect(p.y).toBeGreaterThanOrEqual(0);
    expect(p.y).toBeLessThanOrEqual(height);
  }
  // Кадр не тесный и не пустой: граф занимает заметную часть высоты.
  const ys = points.map((p) => p.y);
  expect(Math.max(...ys) - Math.min(...ys)).toBeGreaterThan(height * 0.5);
});

test("fit одного видимого узла не приближает до абсурда", () => {
  let api: Graph2DApi | null = null;
  const id = graph.nodes()[0]!;
  const lone = buildGraph({
    nodes: generateFixtures().graph.nodes.filter((n) => n.entity_id === id),
    relations: [],
  });
  render(<Graph2D graph={lone} filters={filters()} onSelect={() => {}} onReady={(a) => (api = a)} />);
  api!.fit({ top: 60, right: 0, bottom: 0, left: 292 });
  expect(sigmaState.instances.at(-1)!.camState.ratio).toBeGreaterThanOrEqual(0.1);
});

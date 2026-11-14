import { act, render } from "@testing-library/react";
import type { AbstractGraph } from "graphology-types";
import { generateFixtures } from "../fixtures/generate";
import { Graph2D } from "./Graph2D";
import { buildGraph, DEFAULT_FILTERS, type GraphFilters } from "./model";
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
const sigmaState = vi.hoisted(() => ({
  instances: [] as Array<{ indexed: boolean; killed: boolean; graph: AbstractGraph; handlers: Map<string, Handler[]> }>,
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

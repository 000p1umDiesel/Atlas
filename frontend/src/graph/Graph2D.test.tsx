import { act, render } from "@testing-library/react";
import type { AbstractGraph } from "graphology-types";
import { generateFixtures } from "../fixtures/generate";
import { Graph2D } from "./Graph2D";
import { buildGraph, DEFAULT_FILTERS, type GraphFilters } from "./model";

/**
 * Подмена Sigma 4.0.6 в той части, где живёт ошибка «node can't be repaint».
 * Настоящая Sigma на `eachNodeAttributesUpdated` с подсказкой `attributes`, в которой нет
 * x/y/zIndex/type, пропускает переиндексацию и берёт место узла из `nodeProgramIndex`.
 * Этот индекс пуст, пока ждёт полная переобработка: `scheduleRefresh()` после
 * `window.resize` или `setSetting`, а также после потери WebGL-контекста (render выходит
 * сразу). Тогда Sigma бросает исключение прямо из обработчика события graphology.
 */
const sigmaState = vi.hoisted(() => ({
  instances: [] as Array<{ indexed: boolean; killed: boolean }>,
}));

vi.mock("sigma", () => {
  const LAYOUT = new Set(["x", "y", "zIndex", "type"]);
  class FakeSigma {
    indexed = false;
    killed = false;
    private readonly listener: (e: { hints?: { attributes?: Array<string | number> } }) => void;
    constructor(private readonly graph: AbstractGraph) {
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
    on() {
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

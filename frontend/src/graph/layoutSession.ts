// Ядро воркера раскладки: синхронный ForceAtlas2 пачками. Отдельно от layout.worker.ts,
// чтобы проверять его в тестах без Worker.
import Graph from "graphology";
import forceAtlas2 from "graphology-layout-forceatlas2";
import type { LayoutStartRequest } from "./layoutProtocol";

/** Итераций в одной пачке: после каждой воркер шлёт кадр. */
export const BATCH = 20;
/** Предел времени одного запуска, мс. */
export const TIME_LIMIT_MS = 4000;
/** Барнс-Хат включается на графах крупнее этого. */
const BARNES_HUT_FROM = 500;
/** Сходимость: средний сдвиг узла за пачку меньше этой доли размаха раскладки. */
const CONVERGED = 1e-4;

/** Сколько ещё итераций и времени даёт раскладке каждый сдвиг закреплённого узла. */
const PIN_ITERATIONS = 400;
const PIN_TIME_MS = 2500;

/**
 * Сила притяжения к центру (режим strongGravity: растёт с расстоянием). С мягкой гравитацией
 * `inferSettings` (0.05) несвязные компоненты и одиночные узлы отталкиваются в углы, и кадр
 * «весь граф» становится мелким; с этой — держатся рядом, не сминая компоненты.
 */
export const LAYOUT_GRAVITY = 0.5;

/** Настройки ForceAtlas2 для графа: `inferSettings` + сильная гравитация, Барнс-Хат на крупных. */
export function layoutSettings(g: Graph) {
  return {
    ...forceAtlas2.inferSettings(g),
    strongGravityMode: true,
    gravity: LAYOUT_GRAVITY,
    barnesHutOptimize: g.order > BARNES_HUT_FROM,
  };
}

export interface LayoutSession {
  step(): { positions: Float32Array; done: boolean };
  /** Закрепить узел в (x, y) и продолжить раскладку (перетаскивание). */
  pin(id: string, x: number, y: number): void;
}

export function createLayoutSession(
  req: LayoutStartRequest,
  now: () => number = () => performance.now(),
): LayoutSession {
  const g = new Graph({ multi: true, allowSelfLoops: false });
  const seen = new Set<string>();
  req.nodes.forEach((n, i) => {
    // Совпадающие или нечисловые позиции дают в FA2 деление на ноль: разводим по спирали.
    let { x, y } = n;
    const key = `${x}:${y}`;
    if (!Number.isFinite(x) || !Number.isFinite(y) || seen.has(key)) {
      x = Math.cos(i) * (1 + i * 0.1);
      y = Math.sin(i) * (1 + i * 0.1);
    }
    seen.add(`${x}:${y}`);
    g.mergeNode(n.id, { x, y, size: n.size, fixed: n.fixed === true });
  });
  for (const [src, dst, weight] of req.edges) {
    if (src !== dst && g.hasNode(src) && g.hasNode(dst)) g.addEdge(src, dst, { weight });
  }

  const ids = req.nodes.map((n) => n.id);
  const settings = layoutSettings(g);
  let started = now();
  let timeLimit = TIME_LIMIT_MS;
  let done = g.order === 0;
  let iterations = 0;
  let limit = req.iterations;

  const snapshot = (): Float32Array => {
    const out = new Float32Array(ids.length * 2);
    ids.forEach((id, i) => {
      out[2 * i] = g.getNodeAttribute(id, "x");
      out[2 * i + 1] = g.getNodeAttribute(id, "y");
    });
    return out;
  };

  return {
    step() {
      if (done) return { positions: snapshot(), done: true };
      const before = snapshot();
      const batch = Math.max(1, Math.min(BATCH, limit - iterations));
      forceAtlas2.assign(g, { iterations: batch, settings, getEdgeWeight: "weight" });
      iterations += batch;
      const positions = snapshot();

      let moved = 0;
      let minX = Infinity;
      let maxX = -Infinity;
      let minY = Infinity;
      let maxY = -Infinity;
      for (let i = 0; i < ids.length; i++) {
        const x = positions[2 * i];
        const y = positions[2 * i + 1];
        moved += Math.hypot(x - before[2 * i], y - before[2 * i + 1]);
        minX = Math.min(minX, x);
        maxX = Math.max(maxX, x);
        minY = Math.min(minY, y);
        maxY = Math.max(maxY, y);
      }
      const extent = Math.max(maxX - minX, maxY - minY, 1);
      const converged = moved / ids.length / extent < CONVERGED;
      done = converged || iterations >= limit || now() - started >= timeLimit;
      return { positions, done };
    },
    pin(id, x, y) {
      if (!g.hasNode(id) || !Number.isFinite(x) || !Number.isFinite(y)) return;
      g.mergeNodeAttributes(id, { x, y, fixed: true });
      limit = Math.max(limit, iterations + PIN_ITERATIONS);
      started = now();
      timeLimit = PIN_TIME_MS;
      done = false;
    },
  };
}

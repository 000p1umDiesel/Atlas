import Graph from "graphology";
import forceAtlas2 from "graphology-layout-forceatlas2";
import { createLayoutSession, layoutSettings } from "./layoutSession";

/** Две несвязные «звезды» по 5 узлов, стартуют по спирали золотого угла (как в Graph2D). */
function twoComponents(): Graph {
  const g = new Graph({ multi: true, allowSelfLoops: false });
  const ids = [...["a0", "a1", "a2", "a3", "a4"], ...["b0", "b1", "b2", "b3", "b4"]];
  ids.forEach((id, i) => {
    const r = 10 * Math.sqrt(i + 1);
    g.addNode(id, { x: r * Math.cos(i * 2.399963), y: r * Math.sin(i * 2.399963), size: 8 });
  });
  for (const c of ["a", "b"]) for (let i = 1; i < 5; i++) g.addEdge(`${c}0`, `${c}${i}`, { weight: 1 });
  return g;
}

function centerAndRadius(g: Graph, prefix: string) {
  const pts = g.filterNodes((id) => id.startsWith(prefix)).map((id) => g.getNodeAttributes(id) as { x: number; y: number });
  const cx = pts.reduce((s, p) => s + p.x, 0) / pts.length;
  const cy = pts.reduce((s, p) => s + p.y, 0) / pts.length;
  const radius = pts.reduce((s, p) => s + Math.hypot(p.x - cx, p.y - cy), 0) / pts.length;
  return { cx, cy, radius };
}

test("несвязные компоненты не разъезжаются: центры ближе 4 средних радиусов", () => {
  const g = twoComponents();
  forceAtlas2.assign(g, { iterations: 500, settings: layoutSettings(g), getEdgeWeight: "weight" });
  const a = centerAndRadius(g, "a");
  const b = centerAndRadius(g, "b");
  const distance = Math.hypot(a.cx - b.cx, a.cy - b.cy);
  expect(distance).toBeLessThan(4 * ((a.radius + b.radius) / 2));
  // И не слиплись в точку.
  expect(a.radius).toBeGreaterThan(0);
  expect(b.radius).toBeGreaterThan(0);
});

test("layoutSettings: сильная гравитация, Барнс-Хат только на крупных графах", () => {
  const small = layoutSettings(twoComponents());
  expect(small.strongGravityMode).toBe(true);
  expect(small.barnesHutOptimize).toBe(false);
  const big = new Graph();
  for (let i = 0; i < 600; i++) big.addNode(String(i));
  expect(layoutSettings(big).barnesHutOptimize).toBe(true);
});

test("сессия раскладки сходится и отдаёт позиции всех узлов", () => {
  const g = twoComponents();
  const session = createLayoutSession({
    kind: "start",
    nodes: g.mapNodes((id, a) => ({ id, x: a.x as number, y: a.y as number, size: 8 })),
    edges: g.mapEdges((_, a, s, t) => [s, t, a.weight as number] as [string, string, number]),
    iterations: 500,
  });
  let frame = session.step();
  while (!frame.done) frame = session.step();
  expect(frame.positions).toHaveLength(20);
  expect([...frame.positions].every(Number.isFinite)).toBe(true);
});

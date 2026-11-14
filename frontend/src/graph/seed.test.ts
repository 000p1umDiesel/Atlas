import type { NodeView } from "../api/types";
import { buildGraph } from "./model";
import { seedPositions } from "./seed";

const node = (id: string): NodeView => ({
  entity_id: id,
  name: id,
  type: "Person",
  mention_count: 1,
  pagerank: null,
  community: null,
  degree: 1,
});
const relation = (src: string, dst: string) => ({
  src_id: src,
  src_name: src,
  predicate: "knows",
  dst_id: dst,
  dst_name: dst,
  description: "",
  weight: 1,
  evidence: [],
});

// a — b — c, d отдельно; b и c — новые соседи a.
const g = buildGraph({
  nodes: ["a", "b", "c", "d"].map(node),
  relations: [relation("a", "b"), relation("c", "a")],
});

test("узел с прошлой позицией сохраняет её", () => {
  const seeds = seedPositions(g, new Map([["a", { x: 5, y: -3 }]]));
  expect(seeds.get("a")).toEqual({ x: 5, y: -3 });
});

test("новый сосед размещённого узла — на расстоянии 12 от него, соседи не совпадают", () => {
  const seeds = seedPositions(g, new Map([["a", { x: 5, y: -3 }]]));
  for (const id of ["b", "c"]) {
    const p = seeds.get(id)!;
    expect(Math.hypot(p.x - 5, p.y + 3)).toBeCloseTo(12, 6);
  }
  expect(seeds.get("b")).not.toEqual(seeds.get("c"));
});

test("новый узел без размещённых соседей в карту не попадает", () => {
  const seeds = seedPositions(g, new Map([["a", { x: 5, y: -3 }]]));
  expect(seeds.has("d")).toBe(false);
  expect(seedPositions(g, new Map()).size).toBe(0);
});

test("z переносится, если был; позиции ушедших узлов не нужны", () => {
  const seeds = seedPositions(
    g,
    new Map([
      ["a", { x: 1, y: 2, z: 3 }],
      ["gone", { x: 0, y: 0, z: 0 }],
    ]),
  );
  expect(seeds.get("a")).toEqual({ x: 1, y: 2, z: 3 });
  const b = seeds.get("b")!;
  expect(b.z).toBe(3);
  expect(Math.hypot(b.x - 1, b.y - 2, b.z! - 3)).toBeCloseTo(12, 6);
  expect(seeds.has("gone")).toBe(false);
});

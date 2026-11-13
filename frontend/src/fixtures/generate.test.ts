import { generateFixtures } from "./generate";

describe("generateFixtures", () => {
  const f = generateFixtures(7);
  it("детерминирован", () => {
    expect(generateFixtures(7)).toEqual(f);
  });
  it("id уникальны, рёбра ссылаются на существующие узлы", () => {
    const ids = f.graph.nodes.map((n) => n.entity_id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(ids.length).toBe(300);
    const set = new Set(ids);
    for (const r of f.graph.relations) {
      expect(set.has(r.src_id)).toBe(true);
      expect(set.has(r.dst_id)).toBe(true);
    }
  });
  it("все 8 типов представлены, 12 сообществ", () => {
    for (const t of f.entityTypes) expect(f.graph.nodes.some((n) => n.type === t)).toBe(true);
    expect(f.entityTypes).toHaveLength(8);
    expect(new Set(f.graph.nodes.map((n) => n.community)).size).toBe(12);
  });
  it("статистика согласована с графом (ruling E)", () => {
    expect(f.stats.entities).toBe(300);
    expect(f.stats.relations).toBe(f.graph.relations.length);
    expect(f.types.reduce((s, t) => s + t.count, 0)).toBe(300);
  });
});

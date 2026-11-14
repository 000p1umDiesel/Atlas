import type { NodeView, RelationView } from "../api/types";
import { mergeParts } from "./dataset";

const node = (id: string, name = id): NodeView => ({
  entity_id: id,
  name,
  type: "Person",
  mention_count: 1,
  pagerank: null,
  community: null,
  degree: 1,
});
const rel = (src: string, predicate: string, dst: string, weight = 1): RelationView => ({
  src_id: src,
  src_name: src,
  predicate,
  dst_id: dst,
  dst_name: dst,
  description: "",
  weight,
  evidence: [],
});

describe("mergeParts", () => {
  it("пусто → пустая часть", () => {
    expect(mergeParts([])).toEqual({ nodes: [], relations: [] });
  });
  it("убирает дубликаты узлов и связей, первые поля выигрывают", () => {
    const merged = mergeParts([
      { nodes: [node("a", "Ада"), node("b")], relations: [rel("a", "knows", "b", 2)] },
      { nodes: [node("b", "Другое имя"), node("c")], relations: [rel("a", "knows", "b", 9), rel("b", "knows", "a"), rel("b", "cites", "c")] },
    ]);
    expect(merged.nodes.map((n) => n.entity_id)).toEqual(["a", "b", "c"]);
    expect(merged.nodes[1]!.name).toBe("b");
    expect(merged.relations.map((r) => `${r.src_id}|${r.predicate}|${r.dst_id}`)).toEqual(["a|knows|b", "b|knows|a", "b|cites|c"]);
    expect(merged.relations[0]!.weight).toBe(2);
  });
});

import type { GraphPayload, NodeView } from "../api/types";
import {
  communityLabel,
  communitySummaries,
  predicateCounts,
  predicateLabel,
  rankByPagerank,
  shownTypes,
  topRelations,
  typeCounts,
} from "./insights";
import { buildGraph, DEFAULT_FILTERS } from "./model";

const node = (id: string, type: string, community: string | null, pagerank: number, mentions = 1): NodeView => ({
  entity_id: id,
  name: id.toUpperCase(),
  type,
  mention_count: mentions,
  pagerank,
  community,
  degree: 0,
});

const rel = (src: string, dst: string, predicate: string, weight: number) => ({
  src_id: src,
  src_name: src.toUpperCase(),
  predicate,
  dst_id: dst,
  dst_name: dst.toUpperCase(),
  description: "",
  weight,
  evidence: [],
});

const payload: GraphPayload = {
  nodes: [
    node("a", "Person", "c1", 0.4, 10),
    node("b", "Person", "c1", 0.1, 1),
    node("c", "Concept", "c1", 0.2, 5),
    node("d", "Concept", "c2", 0.3, 2),
    node("e", "Place", null, 0.0, 1),
  ],
  relations: [rel("a", "b", "knows", 0.9), rel("c", "a", "uses", 0.5), rel("a", "d", "cites", 0.2)],
  ranked_by: "pagerank",
  truncated: false,
  total_nodes: 5,
};
const g = buildGraph(payload);

test("сообщества: размер, участники по pagerank, без null", () => {
  const s = communitySummaries(g);
  expect(s.map((c) => [c.id, c.size])).toEqual([
    ["c1", 3],
    ["c2", 1],
  ]);
  expect(s[0]!.top).toEqual(["A", "C", "B"]);
  expect(communityLabel(s[0]!)).toBe("A, C и ещё 1");
  expect(communityLabel(s[1]!)).toBe("D");
});

test("счётчики типов учитывают прочие фильтры, но не фильтр типов", () => {
  const counts = typeCounts(g, { ...DEFAULT_FILTERS, types: ["Place"], minMentions: 2 });
  expect(Object.fromEntries(counts)).toEqual({ Person: 1, Concept: 2 });
});

test("типы чипов и легенды: только с узлами в графе плюс выбранные, в порядке конфигурации", () => {
  const order = ["Organization", "Concept", "Person", "Event", "Place"];
  expect(shownTypes(order, g, [])).toEqual(["Concept", "Person", "Place"]);
  expect(shownTypes(order, g, ["Event"])).toEqual(["Concept", "Person", "Event", "Place"]);
  expect(shownTypes(order, null, ["Event"])).toEqual(["Event"]);
});

test("предикаты: по числу связей во всём графе, счётчик — при прочих фильтрах; выбранный без связей — в конце", () => {
  const twice = buildGraph({ ...payload, relations: [...payload.relations, rel("b", "d", "cites", 0.1)] });
  expect(predicateCounts(twice, DEFAULT_FILTERS)).toEqual([
    { predicate: "cites", count: 2 },
    { predicate: "knows", count: 1 },
    { predicate: "uses", count: 1 },
  ]);
  const filtered = predicateCounts(twice, { ...DEFAULT_FILTERS, types: ["Person"], predicates: ["knows", "works_at"] });
  expect(filtered).toEqual([
    { predicate: "cites", count: 0 },
    { predicate: "knows", count: 1 },
    { predicate: "uses", count: 0 },
    { predicate: "works_at", count: 0 },
  ]);
  expect(predicateLabel("works_at")).toBe("works at");
});

test("топ связей: по весу, с направлением", () => {
  const rels = topRelations(g, "a", 2);
  expect(rels.map((r) => [r.direction, r.predicate, r.otherName, r.weight])).toEqual([
    ["out", "knows", "B", 0.9],
    ["in", "uses", "C", 0.5],
  ]);
});

test("место по pagerank", () => {
  const rank = rankByPagerank(g);
  expect(rank.get("a")).toBe(1);
  expect(rank.get("e")).toBe(5);
});

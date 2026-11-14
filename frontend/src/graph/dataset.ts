// Сборка графа из частей: основная (обзор, окрестность, сообщество, пути) и раскрытия.
import type { GraphPart } from "../api/types";

/** Части в одну: узлы — по `entity_id`, связи — по `src_id|predicate|dst_id`; первая выигрывает. */
export function mergeParts(parts: GraphPart[]): GraphPart {
  const nodes = new Map<string, GraphPart["nodes"][number]>();
  const relations = new Map<string, GraphPart["relations"][number]>();
  for (const part of parts) {
    for (const n of part.nodes) if (!nodes.has(n.entity_id)) nodes.set(n.entity_id, n);
    for (const r of part.relations) {
      const key = `${r.src_id}|${r.predicate}|${r.dst_id}`;
      if (!relations.has(key)) relations.set(key, r);
    }
  }
  return { nodes: [...nodes.values()], relations: [...relations.values()] };
}

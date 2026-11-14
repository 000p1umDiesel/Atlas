// Начальные позиции нового графа из прежнего (раскрытие соседей, смена режима или фильтров):
// раскладка продолжается с того, что уже было на экране, а не «взрывается» с нуля.
import type { Graph } from "./model";

export interface SeedPosition {
  x: number;
  y: number;
  z?: number;
}

/** Расстояние от размещённого соседа, на которое ставится новый узел. */
const SEED_RADIUS = 12;
/** Золотой угол: новые соседи одного узла расходятся веером без совпадений. */
const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));

const finite = (p: SeedPosition) =>
  Number.isFinite(p.x) && Number.isFinite(p.y) && (p.z === undefined || Number.isFinite(p.z));

/**
 * Позиции для узлов `graph`: узел с прошлой позицией сохраняет её; новый узел с уже
 * размещённым соседом встаёт рядом с ним (радиус 12, шаг — золотой угол); прочие в
 * результат не попадают (рендерер ставит их по спирали). `z` переносится, если был.
 */
export function seedPositions(
  graph: Graph,
  previous: ReadonlyMap<string, SeedPosition>,
): Map<string, SeedPosition> {
  const out = new Map<string, SeedPosition>();
  graph.forEachNode((id) => {
    const p = previous.get(id);
    if (p && finite(p)) out.set(id, p.z === undefined ? { x: p.x, y: p.y } : { x: p.x, y: p.y, z: p.z });
  });
  // Сколько новых узлов уже поставлено вокруг каждого соседа (следующий — на новом угле).
  const placedAround = new Map<string, number>();
  graph.forEachNode((id) => {
    if (out.has(id)) return;
    const anchor = graph.findNeighbor(id, (other) => previous.has(other) && out.has(other));
    if (anchor === undefined) return;
    const base = out.get(anchor) as SeedPosition;
    const k = placedAround.get(anchor) ?? 0;
    placedAround.set(anchor, k + 1);
    const angle = k * GOLDEN_ANGLE;
    const seed: SeedPosition = { x: base.x + SEED_RADIUS * Math.cos(angle), y: base.y + SEED_RADIUS * Math.sin(angle) };
    if (base.z !== undefined) seed.z = base.z;
    out.set(id, seed);
  });
  return out;
}

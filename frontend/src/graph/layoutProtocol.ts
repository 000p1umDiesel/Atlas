import type { AbstractGraph, Attributes } from "graphology-types";

/** Узел для раскладки: позиция и радиус (радиус пригодится для adjustSizes). */
export interface LayoutNode {
  id: string;
  x: number;
  y: number;
  size: number;
  /** Закреплён пользователем (перетащен): ForceAtlas2 его не двигает. */
  fixed?: boolean;
}

export type LayoutStartRequest = {
  kind: "start";
  nodes: LayoutNode[];
  /** [источник, приёмник, вес] */
  edges: [string, string, number][];
  iterations: number;
};

/** Перетаскиваемый узел: закрепить в (x, y) и продолжать раскладку вокруг него. */
export type LayoutPinRequest = { kind: "pin"; id: string; x: number; y: number };

export type LayoutRequest = LayoutStartRequest | LayoutPinRequest | { kind: "stop" };

/** Кадр раскладки: x,y подряд в порядке `nodes` запроса; буфер передаётся как transferable. */
export interface LayoutFrame {
  positions: Float32Array;
  /** Последний кадр запуска (сходимость, предел итераций или 4 с). */
  done?: boolean;
}

/** Предел итераций ForceAtlas2 за один запуск (вторая граница — 4 с в воркере). */
export const LAYOUT_ITERATIONS = 2000;

function finite(v: unknown, fallback: number): number {
  return typeof v === "number" && Number.isFinite(v) ? v : fallback;
}

/**
 * Запрос на раскладку только видимой части графа; координаты — текущие атрибуты x/y.
 * Узлы из `fixed` (перетащенные пользователем) помечаются закреплёнными.
 */
export function encodeLayoutRequest(
  graph: AbstractGraph<Attributes, Attributes>,
  visible: { nodes: Set<string>; edges: Set<string> },
  iterations: number = LAYOUT_ITERATIONS,
  fixed: ReadonlySet<string> = new Set(),
): LayoutStartRequest {
  const nodes: LayoutNode[] = [];
  graph.forEachNode((id, a) => {
    if (!visible.nodes.has(id)) return;
    const node: LayoutNode = { id, x: finite(a.x, 0), y: finite(a.y, 0), size: finite(a.size, 1) };
    if (fixed.has(id)) node.fixed = true;
    nodes.push(node);
  });
  const edges: [string, string, number][] = [];
  graph.forEachEdge((key, a, src, dst) => {
    if (!visible.edges.has(key) || src === dst) return;
    if (!visible.nodes.has(src) || !visible.nodes.has(dst)) return;
    const w = finite(a.weight, 1);
    edges.push([src, dst, w > 0 ? w : 1]);
  });
  return { kind: "start", nodes, edges, iterations };
}

/**
 * Записывает кадр в атрибуты x/y одним пакетным обновлением (одно событие для sigma).
 * Кадр неверной длины игнорируется: возвращает false, без исключения.
 * Узлы из `skip` (перетаскиваемый) не трогаются: их ведёт указатель.
 */
export function applyFrame(
  graph: AbstractGraph<Attributes, Attributes>,
  ids: string[],
  frame: LayoutFrame,
  skip?: ReadonlySet<string>,
): boolean {
  const p = frame.positions;
  if (p.length !== ids.length * 2) return false;
  const index = new Map<string, number>();
  ids.forEach((id, i) => index.set(id, i));
  graph.updateEachNodeAttributes(
    (id, attrs) => {
      const i = index.get(id);
      if (i === undefined || skip?.has(id)) return attrs;
      const x = p[2 * i];
      const y = p[2 * i + 1];
      if (!Number.isFinite(x) || !Number.isFinite(y)) return attrs;
      return { ...attrs, x, y };
    },
    { attributes: ["x", "y"] },
  );
  return true;
}

/** То, что нужно контроллеру от Worker (в тестах — подделка). */
export interface WorkerLike {
  postMessage(message: LayoutRequest): void;
  terminate(): void;
  onmessage: ((e: MessageEvent<LayoutFrame>) => void) | null;
  onerror: ((e: ErrorEvent) => void) | null;
}

/**
 * Жизненный цикл воркера раскладки. Каждый запуск — новый воркер, прежний
 * завершается (`terminate`): живых воркеров не больше одного, а запоздавший кадр
 * прежнего запуска не попадёт в новый набор узлов.
 */
export class LayoutController {
  private worker: WorkerLike | null = null;
  private ids: string[] = [];
  private active = false;
  private disposed = false;

  constructor(
    private readonly createWorker: () => WorkerLike,
    private readonly onFrame: (ids: string[], frame: LayoutFrame) => void,
  ) {}

  get running(): boolean {
    return this.active;
  }

  start(request: LayoutStartRequest): void {
    this.kill();
    if (this.disposed || request.nodes.length === 0) return;
    const worker = this.createWorker();
    this.worker = worker;
    this.ids = request.nodes.map((n) => n.id);
    this.active = true;
    worker.onmessage = (e) => {
      if (worker !== this.worker || !this.active) return;
      this.onFrame(this.ids, e.data);
    };
    worker.onerror = () => {
      if (worker === this.worker) this.kill();
    };
    worker.postMessage(request);
  }

  /** Сдвинуть закреплённый узел в идущей раскладке; false — раскладки нет. */
  pin(id: string, x: number, y: number): boolean {
    if (!this.worker || !this.active) return false;
    this.worker.postMessage({ kind: "pin", id, x, y });
    return true;
  }

  /** Остановить раскладку (перетаскивание узла): воркер жив, но кадры больше не применяются. */
  stop(): void {
    if (!this.worker || !this.active) return;
    this.active = false;
    this.worker.postMessage({ kind: "stop" });
  }

  dispose(): void {
    this.disposed = true;
    this.kill();
  }

  private kill(): void {
    this.active = false;
    if (!this.worker) return;
    this.worker.onmessage = null;
    this.worker.onerror = null;
    this.worker.terminate();
    this.worker = null;
  }
}

/** Сдвиг указателя (px), после которого нажатие на узел считается перетаскиванием, а не кликом. */
export const DRAG_THRESHOLD_PX = 4;

/**
 * Раскладка и перетаскивание узлов. Sigma начинает перетаскивание с первого же движения
 * указателя, поэтому перетаскиванием считается только сдвиг больше порога (клик с дрожанием
 * ничего не трогает). Перетащенные узлы остаются закреплёнными (`pinned`).
 *
 * Живой режим (по умолчанию): раскладка во время перетаскивания продолжается, узел закреплён
 * в позиции указателя (`follow` → `pin`), соседи плавно тянутся следом. Без живого режима
 * (prefers-reduced-motion) раскладка на время перетаскивания останавливается и
 * возобновляется после отпускания с текущих позиций.
 */
export class LayoutDragGuard {
  readonly pinned = new Set<string>();
  private origin: { x: number; y: number } | null = null;
  private node: string | null = null;
  private active = false;
  private readonly live: boolean;

  constructor(
    private readonly controller: Pick<LayoutController, "start" | "stop" | "pin" | "running">,
    private readonly encode: (pinned: ReadonlySet<string>) => LayoutStartRequest,
    options: { live?: boolean } = {},
  ) {
    this.live = options.live ?? true;
  }

  /** Идёт перетаскивание (сдвиг больше порога). */
  get dragging(): boolean {
    return this.active;
  }

  start(x: number, y: number, node: string | null = null): void {
    this.origin = { x, y };
    this.node = node;
    this.active = false;
  }

  move(x: number, y: number): void {
    if (!this.origin || this.active) return;
    if (Math.hypot(x - this.origin.x, y - this.origin.y) <= DRAG_THRESHOLD_PX) return;
    this.active = true;
    if (!this.live) {
      this.controller.stop();
      return;
    }
    if (this.node !== null) this.pinned.add(this.node);
    if (!this.controller.running) this.controller.start(this.encode(this.pinned));
  }

  /** Текущая (сглаженная) позиция перетаскиваемого узла — в идущую раскладку. */
  follow(node: string, x: number, y: number): void {
    if (!this.active || !this.live) return;
    if (!this.controller.pin(node, x, y)) this.controller.start(this.encode(this.pinned));
  }

  end(nodes: string[]): void {
    const dragged = this.active;
    this.origin = null;
    this.node = null;
    this.active = false;
    if (!dragged) return;
    for (const n of nodes) this.pinned.add(n);
    if (!this.live) this.controller.start(this.encode(this.pinned));
  }
}

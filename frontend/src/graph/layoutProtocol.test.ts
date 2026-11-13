import { MultiDirectedGraph } from "graphology";
import {
  applyFrame,
  encodeLayoutRequest,
  LayoutController,
  LayoutDragGuard,
  type LayoutFrame,
  type LayoutRequest,
  type LayoutStartRequest,
  type WorkerLike,
} from "./layoutProtocol";
import { createLayoutSession } from "./layoutSession";

type N = { x: number; y: number; size: number };
type E = { weight: number };

function sample() {
  const g = new MultiDirectedGraph<N, E>();
  g.addNode("a", { x: 1, y: 2, size: 5 });
  g.addNode("b", { x: 3, y: 4, size: 6 });
  g.addNode("c", { x: 5, y: 6, size: 7 });
  g.addEdgeWithKey("a|r|b", "a", "b", { weight: 2.5 });
  g.addEdgeWithKey("b|r|c", "b", "c", { weight: 1 });
  g.addEdgeWithKey("a|r|c", "a", "c", { weight: 4 });
  return g;
}

describe("encodeLayoutRequest", () => {
  it("берёт только видимые узлы и рёбра, веса из атрибута weight", () => {
    const g = sample();
    const req = encodeLayoutRequest(g, { nodes: new Set(["a", "b"]), edges: new Set(["a|r|b", "a|r|c"]) });
    expect(req.kind).toBe("start");
    expect(req.nodes).toEqual([
      { id: "a", x: 1, y: 2, size: 5 },
      { id: "b", x: 3, y: 4, size: 6 },
    ]);
    // a|r|c видимо по ключу, но c скрыт — ребро не попадает в раскладку
    expect(req.edges).toEqual([["a", "b", 2.5]]);
    expect(req.iterations).toBeGreaterThan(0);
  });

  it("неверный вес заменяется единицей, петли пропускаются", () => {
    const g = sample();
    g.addEdgeWithKey("a|self|a", "a", "a", { weight: 3 });
    g.setEdgeAttribute("b|r|c", "weight", Number.NaN);
    const req = encodeLayoutRequest(g, {
      nodes: new Set(["a", "b", "c"]),
      edges: new Set(["b|r|c", "a|self|a"]),
    });
    expect(req.edges).toEqual([["b", "c", 1]]);
  });

  it("пустое видимое множество → пустой запрос", () => {
    const req = encodeLayoutRequest(sample(), { nodes: new Set(), edges: new Set() });
    expect(req.nodes).toEqual([]);
    expect(req.edges).toEqual([]);
  });
});

describe("encodeLayoutRequest: закреплённые узлы", () => {
  it("помечает fixed только узлы из набора", () => {
    const req = encodeLayoutRequest(
      sample(),
      { nodes: new Set(["a", "b"]), edges: new Set() },
      50,
      new Set(["b", "gone"]),
    );
    expect(req.nodes).toEqual([
      { id: "a", x: 1, y: 2, size: 5 },
      { id: "b", x: 3, y: 4, size: 6, fixed: true },
    ]);
    expect(req.iterations).toBe(50);
  });
});

describe("applyFrame", () => {
  it("записывает x/y по индексам ids", () => {
    const g = sample();
    const ok = applyFrame(g, ["c", "a"], { positions: new Float32Array([10, 20, 30, 40]) });
    expect(ok).toBe(true);
    expect(g.getNodeAttributes("c")).toMatchObject({ x: 10, y: 20 });
    expect(g.getNodeAttributes("a")).toMatchObject({ x: 30, y: 40 });
    expect(g.getNodeAttributes("b")).toMatchObject({ x: 3, y: 4 });
  });

  it("кадр неверной длины игнорируется без исключения", () => {
    const g = sample();
    expect(() => applyFrame(g, ["a", "b"], { positions: new Float32Array([1, 2, 3]) })).not.toThrow();
    expect(applyFrame(g, ["a", "b"], { positions: new Float32Array([1, 2, 3]) })).toBe(false);
    expect(g.getNodeAttributes("a")).toMatchObject({ x: 1, y: 2 });
  });

  it("не трогает узлы из skip (перетаскиваемый узел ведёт указатель, а не кадр)", () => {
    const g = sample();
    applyFrame(g, ["a", "b"], { positions: new Float32Array([10, 20, 30, 40]) }, new Set(["a"]));
    expect(g.getNodeAttributes("a")).toMatchObject({ x: 1, y: 2 });
    expect(g.getNodeAttributes("b")).toMatchObject({ x: 30, y: 40 });
  });

  it("пропускает исчезнувшие узлы и нечисловые координаты", () => {
    const g = sample();
    applyFrame(g, ["gone", "b"], { positions: new Float32Array([1, 1, Number.NaN, 9]) });
    expect(g.getNodeAttributes("b")).toMatchObject({ x: 3, y: 4 });
  });
});

class FakeWorker implements WorkerLike {
  static all: FakeWorker[] = [];
  sent: LayoutRequest[] = [];
  terminated = false;
  onmessage: ((e: MessageEvent<LayoutFrame>) => void) | null = null;
  onerror: ((e: ErrorEvent) => void) | null = null;
  constructor() {
    FakeWorker.all.push(this);
  }
  postMessage(msg: LayoutRequest) {
    this.sent.push(msg);
  }
  terminate() {
    this.terminated = true;
  }
  emit(positions: number[]) {
    this.onmessage?.({ data: { positions: new Float32Array(positions) } } as MessageEvent<LayoutFrame>);
  }
}

describe("LayoutController", () => {
  beforeEach(() => {
    FakeWorker.all = [];
  });
  const req = (ids: string[]): LayoutStartRequest => ({
    kind: "start",
    nodes: ids.map((id, i) => ({ id, x: i, y: i, size: 1 })),
    edges: [],
    iterations: 100,
  });
  const alive = () => FakeWorker.all.filter((w) => !w.terminated);

  it("перезапуск завершает прежний воркер: живых не больше одного", () => {
    const frames: string[][] = [];
    const c = new LayoutController(() => new FakeWorker(), (ids) => frames.push(ids));
    c.start(req(["a"]));
    c.start(req(["a", "b"]));
    c.start(req(["c"]));
    expect(FakeWorker.all).toHaveLength(3);
    expect(alive()).toHaveLength(1);
    expect(alive()[0].sent).toEqual([req(["c"])]);
  });

  it("кадры старого воркера не применяются", () => {
    const frames: string[][] = [];
    const c = new LayoutController(() => new FakeWorker(), (ids) => frames.push(ids));
    c.start(req(["a"]));
    const old = FakeWorker.all[0];
    c.start(req(["b"]));
    old.emit([1, 1]);
    FakeWorker.all[1].emit([2, 2]);
    expect(frames).toEqual([["b"]]);
  });

  it("stop шлёт {kind: stop} и глушит дальнейшие кадры", () => {
    const frames: string[][] = [];
    const c = new LayoutController(() => new FakeWorker(), (ids) => frames.push(ids));
    c.start(req(["a"]));
    c.stop();
    FakeWorker.all[0].emit([1, 1]);
    expect(FakeWorker.all[0].sent.at(-1)).toEqual({ kind: "stop" });
    expect(frames).toEqual([]);
    expect(c.running).toBe(false);
  });

  it("пустой запрос не создаёт воркер; dispose завершает текущий", () => {
    const c = new LayoutController(() => new FakeWorker(), () => {});
    c.start(req([]));
    expect(FakeWorker.all).toHaveLength(0);
    c.start(req(["a"]));
    c.dispose();
    expect(alive()).toHaveLength(0);
    c.start(req(["b"]));
    expect(alive()).toHaveLength(0);
  });

  it("pin передаёт воркеру позицию закреплённого узла, пока раскладка идёт", () => {
    const c = new LayoutController(() => new FakeWorker(), () => {});
    expect(c.pin("a", 1, 2)).toBe(false);
    c.start(req(["a"]));
    expect(c.pin("a", 5, 6)).toBe(true);
    expect(FakeWorker.all[0].sent.at(-1)).toEqual({ kind: "pin", id: "a", x: 5, y: 6 });
    c.stop();
    expect(c.pin("a", 7, 8)).toBe(false);
  });

  it("ошибка воркера останавливает раскладку и завершает его", () => {
    const c = new LayoutController(() => new FakeWorker(), () => {});
    c.start(req(["a"]));
    FakeWorker.all[0].onerror?.({} as ErrorEvent);
    expect(c.running).toBe(false);
    expect(alive()).toHaveLength(0);
  });
});

describe("LayoutDragGuard", () => {
  beforeEach(() => {
    FakeWorker.all = [];
  });
  const visible = { nodes: new Set(["a", "b", "c"]), edges: new Set(["a|r|b", "b|r|c"]) };
  const setup = () => {
    const g = sample();
    const controller = new LayoutController(() => new FakeWorker(), () => {});
    const guard = new LayoutDragGuard(controller, (pinned) => encodeLayoutRequest(g, visible, 100, pinned), {
      live: false,
    });
    controller.start(encodeLayoutRequest(g, visible, 100, guard.pinned));
    return { g, controller, guard };
  };
  const alive = () => FakeWorker.all.filter((w) => !w.terminated);

  it("начало и конец перетаскивания возобновляют раскладку: новый воркер с текущих позиций, узел закреплён", () => {
    const { g, controller, guard } = setup();
    guard.start(100, 100);
    guard.move(130, 120);
    expect(FakeWorker.all[0].sent.at(-1)).toEqual({ kind: "stop" });
    expect(controller.running).toBe(false);
    g.mergeNodeAttributes("a", { x: 42, y: -7 }); // так Sigma двигает узел при перетаскивании
    guard.end(["a"]);
    expect(FakeWorker.all).toHaveLength(2);
    expect(alive()).toEqual([FakeWorker.all[1]]);
    expect(controller.running).toBe(true);
    const req = FakeWorker.all[1].sent[0];
    expect(req.kind).toBe("start");
    if (req.kind !== "start") return;
    expect(req.nodes.find((n) => n.id === "a")).toEqual({ id: "a", x: 42, y: -7, size: 5, fixed: true });
    expect(req.nodes.find((n) => n.id === "b")?.fixed).toBeUndefined();
    expect(guard.pinned.has("a")).toBe(true);
  });

  it("клик с дрожанием в пару пикселей не останавливает раскладку", () => {
    const { controller, guard } = setup();
    guard.start(100, 100);
    guard.move(101, 102);
    guard.end(["a"]);
    expect(FakeWorker.all).toHaveLength(1);
    expect(FakeWorker.all[0].sent).toHaveLength(1);
    expect(controller.running).toBe(true);
    expect(guard.pinned.size).toBe(0);
  });

  it("конец без начала ничего не делает", () => {
    const { guard } = setup();
    guard.move(500, 500);
    guard.end(["a"]);
    expect(FakeWorker.all).toHaveLength(1);
  });
});

describe("LayoutDragGuard: живая раскладка во время перетаскивания", () => {
  beforeEach(() => {
    FakeWorker.all = [];
  });
  const visible = { nodes: new Set(["a", "b", "c"]), edges: new Set(["a|r|b", "b|r|c"]) };
  const setup = (startLayout = true) => {
    const g = sample();
    const controller = new LayoutController(() => new FakeWorker(), () => {});
    const guard = new LayoutDragGuard(controller, (pinned) => encodeLayoutRequest(g, visible, 100, pinned));
    if (startLayout) controller.start(encodeLayoutRequest(g, visible, 100, guard.pinned));
    return { g, controller, guard };
  };

  it("раскладка не останавливается: узел закреплён и ведётся через pin", () => {
    const { controller, guard } = setup();
    guard.start(100, 100, "a");
    guard.move(130, 120);
    expect(controller.running).toBe(true);
    expect(FakeWorker.all).toHaveLength(1);
    expect(guard.pinned.has("a")).toBe(true);
    expect(guard.dragging).toBe(true);
    guard.follow("a", 42, -7);
    expect(FakeWorker.all[0].sent.at(-1)).toEqual({ kind: "pin", id: "a", x: 42, y: -7 });
    guard.end(["a"]);
    expect(guard.dragging).toBe(false);
    expect(guard.pinned.has("a")).toBe(true);
    expect(FakeWorker.all.filter((w) => w.sent.some((m) => m.kind === "stop"))).toHaveLength(0);
  });

  it("если раскладки нет — перетаскивание запускает её с закреплённым узлом", () => {
    const { controller, guard } = setup(false);
    guard.start(0, 0, "b");
    guard.move(50, 0);
    expect(controller.running).toBe(true);
    const req = FakeWorker.all[0].sent[0];
    expect(req.kind === "start" && req.nodes.find((n) => n.id === "b")?.fixed).toBe(true);
  });

  it("отпускание закрепляет узел в цели отпускания и ведёт его, пока догоняет курсор", () => {
    const { guard } = setup();
    const worker = FakeWorker.all[0];
    guard.start(100, 100, "a");
    guard.move(160, 100);
    guard.follow("a", 10, 10); // сглаженная позиция отстаёт от курсора
    guard.end(["a"], { a: { x: 50, y: 60 } });
    // Сразу после отпускания воркер держит узел в цели, а не в отставшей позиции.
    expect(worker.sent.at(-1)).toEqual({ kind: "pin", id: "a", x: 50, y: 60 });
    // Догоняющее сглаживание после отпускания продолжает вести узел…
    guard.follow("a", 40, 48);
    expect(worker.sent.at(-1)).toEqual({ kind: "pin", id: "a", x: 40, y: 48 });
    // …и, догнав, закрепляет ровно в цели; дальше follow ничего не шлёт.
    guard.settle("a", 50, 60);
    expect(worker.sent.at(-1)).toEqual({ kind: "pin", id: "a", x: 50, y: 60 });
    const count = worker.sent.length;
    guard.follow("a", 1, 1);
    expect(worker.sent).toHaveLength(count);
  });

  it("после отпускания кадр раскладки не возвращает узел назад", () => {
    const g = sample();
    const sent: LayoutRequest[] = [];
    // Воркер «по-настоящему»: сессия раскладки, запросы применяются сразу.
    let session: ReturnType<typeof createLayoutSession> | null = null;
    const controller = new LayoutController(
      () => ({
        onmessage: null,
        onerror: null,
        terminate() {},
        postMessage(msg: LayoutRequest) {
          sent.push(msg);
          if (msg.kind === "start") session = createLayoutSession(msg);
          if (msg.kind === "pin") session?.pin(msg.id, msg.x, msg.y);
        },
      }),
      () => {},
    );
    const guard = new LayoutDragGuard(controller, (pinned) => encodeLayoutRequest(g, visible, 100, pinned));
    controller.start(encodeLayoutRequest(g, visible, 100, guard.pinned));
    guard.start(0, 0, "a");
    guard.move(50, 0);
    guard.follow("a", 5, 5);
    guard.end(["a"], { a: { x: 50, y: 60 } });
    const frame = (session as unknown as ReturnType<typeof createLayoutSession>).step();
    const ids = ["a", "b", "c"];
    const i = ids.indexOf("a");
    expect(frame.positions[2 * i]).toBeCloseTo(50, 4);
    expect(frame.positions[2 * i + 1]).toBeCloseTo(60, 4);
  });

  it("клик с дрожанием ничего не закрепляет и не шлёт pin", () => {
    const { guard } = setup();
    guard.start(100, 100, "a");
    guard.move(101, 101);
    guard.follow("a", 1, 1);
    guard.end(["a"]);
    expect(guard.pinned.size).toBe(0);
    expect(FakeWorker.all[0].sent).toHaveLength(1);
  });
});

describe("createLayoutSession (ядро воркера)", () => {
  const start = (n: number, iterations = 100): LayoutStartRequest => ({
    kind: "start",
    nodes: Array.from({ length: n }, (_, i) => ({
      id: `n${i}`,
      x: Math.cos(i) * 10,
      y: Math.sin(i) * 10,
      size: 4,
    })),
    edges: Array.from({ length: Math.max(0, n - 1) }, (_, i) => [`n${i}`, `n${i + 1}`, 1 + i] as [string, string, number]),
    iterations,
  });

  it("каждый шаг даёт кадр 2×n конечных чисел", () => {
    const s = createLayoutSession(start(30));
    const { positions } = s.step();
    expect(positions).toBeInstanceOf(Float32Array);
    expect(positions.length).toBe(60);
    expect(positions.every(Number.isFinite)).toBe(true);
  });

  it("останавливается по числу итераций (пачками по 20)", () => {
    const s = createLayoutSession(start(10, 50));
    const steps = [s.step().done, s.step().done, s.step().done];
    expect(steps).toEqual([false, false, true]);
  });

  it("останавливается по времени (4 с)", () => {
    let t = 0;
    const s = createLayoutSession(start(10, 1e6), () => t);
    expect(s.step().done).toBe(false);
    t = 4001;
    expect(s.step().done).toBe(true);
  });

  it("закреплённый узел не двигается", () => {
    const req = start(12);
    req.nodes[3].fixed = true;
    const { positions } = createLayoutSession(req).step();
    expect(positions[6]).toBeCloseTo(req.nodes[3].x, 5);
    expect(positions[7]).toBeCloseTo(req.nodes[3].y, 5);
    expect(positions[0]).not.toBeCloseTo(req.nodes[0].x, 5);
  });

  it("pin двигает закреплённый узел и снова запускает завершённую раскладку", () => {
    const s = createLayoutSession(start(10, 20));
    expect(s.step().done).toBe(true);
    s.pin("n2", 123, -45);
    const { positions, done } = s.step();
    expect(done).toBe(false);
    expect(positions[4]).toBeCloseTo(123, 5);
    expect(positions[5]).toBeCloseTo(-45, 5);
    s.pin("missing", 1, 1); // неизвестный узел игнорируется
  });

  it("пустой граф сразу завершён; совпадающие позиции не дают NaN", () => {
    expect(createLayoutSession(start(0)).step()).toMatchObject({ done: true });
    const req = start(5);
    for (const n of req.nodes) {
      n.x = 0;
      n.y = 0;
    }
    expect(createLayoutSession(req).step().positions.every(Number.isFinite)).toBe(true);
  });
});

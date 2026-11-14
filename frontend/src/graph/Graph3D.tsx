import { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  AmbientLight,
  CanvasTexture,
  Color,
  DirectionalLight,
  Group,
  LinearFilter,
  PerspectiveCamera,
  Raycaster,
  Scene,
  Sprite,
  SpriteMaterial,
  SRGBColorSpace,
  Vector2,
  Vector3,
  WebGLRenderer,
  type BufferGeometry,
  type Material,
  type Object3D,
  type Texture,
} from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import ThreeForceGraph from "three-forcegraph";
import { cssColor, isDarkScheme, mixRgb, withAlpha } from "../lib/entityColors";
import { fontsReady, graphFont, loadFonts } from "../lib/fonts";
import { prefersReducedMotion } from "../lib/motion";
import type { FocusOffset, Graph2DApi, Graph2DProps } from "./Graph2D";
import { GraphTooltip, placeTooltip, type TooltipInfo } from "./GraphTooltip";
import { NO_INSETS, usableInsets, type FitInsets } from "./interaction";
import { DEFAULT_FILTERS, visibleSubgraph } from "./model";
import { seedPositions, type SeedPosition } from "./seed";
import {
  boundingSphere,
  compositionKey,
  easeInOutCubic,
  fitDistance,
  highlightMatches,
  labelCandidates,
  linkAlpha,
  reconcileNodes,
  toForceData,
  type ForceData,
  type ForceLink,
  type ForceNode,
} from "./scene3d";

/** Тот же контракт, что у 2D: вид можно подменять без изменений вокруг. */
export type Graph3DApi = Graph2DApi;

export interface Graph3DProps extends Graph2DProps {
  onReady?(api: Graph3DApi): void;
  /** Медленное вращение вокруг центра (при prefers-reduced-motion не включается). */
  autoRotate?: boolean;
}

/** Мировой радиус узла на единицу `nodeSize` (4…22 → 2…11; на маленьком графе 7…18 → 3.5…9). */
const NODE_SCALE = 0.5;
const FOV = 50;
/** Подписей без наведения: больше — и в объёме они налезают друг на друга. */
const LABEL_LIMIT = 14;
/** Сколько совпадений поиска подписывать сверх топа (крупнейшие). */
const MATCH_LABEL_LIMIT = 60;
const MATCH_SCALE = 1.35;
const HOVER_SCALE = 1.15;
const FLY_MS = 600;
/** Постоянная времени, с которой камера догоняет выбранный узел, пока симуляция его двигает, мс. */
const FOLLOW_TAU_MS = 160;
const FIT_MS = 400;
/** Сфера по габаритам консервативна: облако в проекции заметно меньше её. */
const FIT_MARGIN = 0.95;
/** Высота строки подписи на экране, CSS-пиксели. */
const LABEL_PX = 13;
/** Текстура подписи рисуется с двойной плотностью. */
const LABEL_DENSITY = 2;
const LABEL_MAX_CHARS = 40;
/** Сдвиг клика, после которого это уже вращение камеры, а не выбор узла. */
const CLICK_SLOP_PX = 5;
/** Два клика по одному узлу не дольше этого — раскрыть его соседей. */
const DOUBLE_CLICK_MS = 300;
/** Тиков раскладки, если анимация выключена: считается сразу, показывается итог. */
const WARMUP_TICKS = 400;

const GRAVITY = 0.04;

/** Слабая пружина каждого узла к началу координат (как forceX/Y/Z из d3, без зависимости). */
function gravityForce(strength: number) {
  let nodes: ForceNode[] = [];
  const force = (alpha: number) => {
    const k = strength * alpha;
    for (const n of nodes) {
      n.vx = (n.vx ?? 0) - (n.x ?? 0) * k;
      n.vy = (n.vy ?? 0) - (n.y ?? 0) * k;
      n.vz = (n.vz ?? 0) - (n.z ?? 0) * k;
    }
  };
  force.initialize = (list: ForceNode[]) => {
    nodes = list;
  };
  return force;
}

type NodeObj = Object3D & { __data?: ForceNode; __graphObjType?: string };
type BoundNode = ForceNode & { __threeObj?: Object3D };
type ForceGraph = ThreeForceGraph<ForceNode, ForceLink>;

interface Palette {
  bg: string;
  ink: string;
  edge: string;
  edgeActive: string;
}

function readPalette(): Palette {
  return {
    bg: cssColor("--card"),
    ink: cssColor("--ink"),
    edge: cssColor("--muted"),
    edgeActive: isDarkScheme() ? cssColor("--primary-ink") : cssColor("--primary-deep"),
  };
}

/** Доля фона в цвете приглушённого узла (при наведении на другой). */
const DIM_MIX = 0.82;
/** Шаг приближения кнопками «+ / −». */
const ZOOM_FACTOR = 1.5;
const LABEL_FONTS = [graphFont(500, 12), graphFont(650, 12)];

const endId = (end: string | { id?: string }) => (typeof end === "object" ? (end.id ?? "") : end);

function nodeRadius(n: ForceNode): number {
  return n.val * NODE_SCALE;
}

interface Label {
  key: string;
  sprite: Sprite;
  material: SpriteMaterial;
  texture: Texture;
  /** Ширина/высота текстуры. */
  aspect: number;
}

/** Подпись-спрайт: текст на canvas (недоверенное имя рисуется как текст, без разметки). */
function createLabel(name: string, palette: Palette, strong: boolean): Label {
  const text = name.length > LABEL_MAX_CHARS ? `${name.slice(0, LABEL_MAX_CHARS - 1)}…` : name;
  const fontPx = 12.5 * LABEL_DENSITY;
  const font = `${strong ? 650 : 500} ${fontPx}px "Golos Text Variable", ui-sans-serif, system-ui, sans-serif`;
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d");
  const pad = 4 * LABEL_DENSITY;
  canvas.height = LABEL_PX * LABEL_DENSITY + pad;
  if (ctx) {
    ctx.font = font;
    canvas.width = Math.max(2, Math.ceil(ctx.measureText(text).width) + pad * 2);
    // После смены размера контекст сбрасывается — шрифт ставится заново.
    ctx.font = font;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.lineJoin = "round";
    ctx.lineWidth = 3 * LABEL_DENSITY;
    ctx.strokeStyle = withAlpha(palette.bg, 0.9);
    ctx.fillStyle = palette.ink;
    const x = canvas.width / 2;
    const y = canvas.height / 2;
    ctx.strokeText(text, x, y);
    ctx.fillText(text, x, y);
  }
  const texture = new CanvasTexture(canvas);
  texture.colorSpace = SRGBColorSpace;
  texture.minFilter = LinearFilter;
  texture.generateMipmaps = false;
  const material = new SpriteMaterial({
    map: texture,
    transparent: true,
    depthWrite: false,
    // Подпись поверх шаров: иначе её закрывает ближний (часто приглушённый) узел.
    depthTest: false,
    sizeAttenuation: false,
  });
  const sprite = new Sprite(material);
  sprite.center.set(0.5, 0);
  sprite.renderOrder = 20;
  return { key: "", sprite, material, texture, aspect: canvas.width / canvas.height };
}

function disposeLabel(label: Label) {
  label.sprite.removeFromParent();
  label.material.dispose();
  label.texture.dispose();
}

interface Flight {
  fromPos: Vector3;
  fromTarget: Vector3;
  toPos: Vector3;
  toTarget: Vector3;
  start: number;
  duration: number;
}

interface SceneCallbacks {
  onSelect(id: string): void;
  /** Двойной клик по узлу: добавить его соседей на граф. */
  onExpand(id: string): void;
  /** Раскладка остановилась: позиции узлов можно запомнить для следующей сцены. */
  onSettle(): void;
  onBackground(): void;
  onHover(node: ForceNode | null): void;
  tooltip(): HTMLElement | null;
}

/**
 * Императивная часть: рендерер, камера, OrbitControls, three-forcegraph, подписи,
 * наведение, выбор, перелёты. Один экземпляр — один WebGL-контекст; `dispose()`
 * освобождает всё и безопасен при повторном вызове.
 */
class Scene3D {
  private readonly renderer: WebGLRenderer;
  private readonly scene = new Scene();
  private readonly camera = new PerspectiveCamera(FOV, 1, 0.5, 200000);
  private readonly controls: OrbitControls;
  private readonly fg: ForceGraph;
  private readonly labelLayer = new Group();
  private readonly labels = new Map<string, Label>();
  private readonly raycaster = new Raycaster();
  private readonly ndc = new Vector2();
  private readonly resizer: ResizeObserver | null;
  private readonly reduced = prefersReducedMotion();
  private palette = readPalette();
  private nodes = new Map<string, ForceNode>();
  private compKey: string | null = null;
  private maxWeight = 0;
  private matches = new Set<string>();
  private hovered: string | null = null;
  private selected: string | null = null;
  private highlight = "";
  private emphasis: ReadonlySet<string> | null = null;
  /** Соседи каждого видимого узла (связи без направления). */
  private adjacency = new Map<string, Set<string>>();
  /** Подсвеченные узлы (окрестность наведения/выбора или пункт легенды); null — всё как есть. */
  private active: Set<string> | null = null;
  /** Центр окрестности: его связи выделены. */
  private center: string | null = null;
  private flight: Flight | null = null;
  /**
   * Наведение на узел, которое ещё не завершено: у узла нет позиции (сцена только создана)
   * или симуляция его двигает. Камера ведёт узел до остановки движка или жеста пользователя.
   */
  private tracking: { id: string; offset: FocusOffset; placed: boolean } | null = null;
  private lastTrack = 0;
  /** Камеру трогал пользователь: автокадрирование больше не вмешивается. */
  private userMoved = false;
  private engineRunning = false;
  private dirty = true;
  private frame = 0;
  private labelUnit = 0;
  private pointer = { x: 0, y: 0, inside: false };
  private press: { x: number; y: number; id: number } | null = null;
  /** Прошлый клик по узлу: второй по тому же узлу за 300 мс — двойной. */
  private lastClick: { id: string; time: number } | null = null;
  /** Начальные позиции узлов из прежней сцены (`seedPositions`); нужны только первому составу. */
  private seeds: ReadonlyMap<string, SeedPosition> | undefined;
  private disposed = false;
  private readonly tmp = new Vector3();
  private readonly up = new Vector3();

  constructor(
    private readonly container: HTMLElement,
    private readonly callbacks: SceneCallbacks,
  ) {
    this.renderer = new WebGLRenderer({ antialias: true, alpha: false });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    const canvas = this.renderer.domElement;
    canvas.className = "absolute inset-0 block h-full w-full";
    container.appendChild(canvas);

    this.scene.background = new Color(this.palette.bg);
    this.scene.add(new AmbientLight(0xffffff, 2.2));
    const sun = new DirectionalLight(0xffffff, 1.4);
    sun.position.set(1, 2, 3);
    this.camera.add(sun);
    this.scene.add(this.camera);
    this.camera.position.set(0, 0, 400);

    this.controls = new OrbitControls(this.camera, canvas);
    this.controls.enableDamping = !this.reduced;
    // Малое затухание — мягкое вращение и зум с инерцией после отпускания.
    this.controls.dampingFactor = 0.055;
    this.controls.autoRotateSpeed = 0.8;
    this.controls.addEventListener("change", this.onControlsChange);
    this.controls.addEventListener("start", this.onControlsStart);

    this.fg = new ThreeForceGraph<ForceNode, ForceLink>()
      .nodeId("id")
      .nodeRelSize(1)
      // three-forcegraph берёт радиус как cbrt(val) · relSize — передаём куб радиуса.
      .nodeVal((n) => Math.pow(nodeRadius(n), 3))
      .nodeColor(this.nodeColorFn())
      .nodeOpacity(1)
      .nodeResolution(14)
      .linkWidth(0)
      .linkOpacity(1)
      .linkColor(this.linkColorFn())
      .warmupTicks(this.reduced ? WARMUP_TICKS : 0)
      .cooldownTicks(this.reduced ? 0 : Infinity)
      .cooldownTime(10000)
      // Остановка по затуханию: иначе симуляция крутится все cooldownTime.
      .d3AlphaMin(0.002)
      .onEngineTick(this.onEngineTick)
      .onEngineStop(this.onEngineStop)
      .onFinishUpdate(this.onFinishUpdate);
    this.fg.d3Force("link")?.distance?.(36);
    // Отталкивание только на средних расстояниях, плюс слабое притяжение к центру:
    // иначе узлы без связей улетают далеко и кадр по всем узлам становится мелким.
    this.fg.d3Force("charge")?.strength?.(-45)?.distanceMax?.(260);
    this.fg.d3Force("gravity", gravityForce(GRAVITY));
    this.scene.add(this.fg);
    this.scene.add(this.labelLayer);

    canvas.addEventListener("pointermove", this.onPointerMove);
    canvas.addEventListener("pointerleave", this.onPointerLeave);
    canvas.addEventListener("pointerdown", this.onPointerDown);
    canvas.addEventListener("pointerup", this.onPointerUp);
    canvas.addEventListener("pointercancel", this.onPointerCancel);

    this.resizer = typeof ResizeObserver === "function" ? new ResizeObserver(() => this.resize()) : null;
    this.resizer?.observe(container);
    this.resize();
    this.frame = requestAnimationFrame(this.loop);
  }

  // ——— данные ———

  /** Начальные позиции из прежней сцены: раскладка продолжается, а не начинается с нуля. */
  setSeeds(seeds: ReadonlyMap<string, SeedPosition>) {
    this.seeds = seeds.size > 0 ? seeds : undefined;
  }

  /** Текущие позиции узлов (для следующей сцены). */
  positions(): Map<string, SeedPosition> {
    const out = new Map<string, SeedPosition>();
    for (const n of this.nodes.values()) {
      if (Number.isFinite(n.x) && Number.isFinite(n.y) && Number.isFinite(n.z)) {
        out.set(n.id, { x: n.x as number, y: n.y as number, z: n.z as number });
      }
    }
    return out;
  }

  setData(data: ForceData) {
    if (this.disposed) return;
    const key = compositionKey(data);
    // Был пустой кадр — новый состав снова кадрируется автоматически.
    if (this.nodes.size === 0) this.userMoved = false;
    const nodes = reconcileNodes(this.nodes, data.nodes, this.seeds);
    this.nodes = new Map(nodes.map((n) => [n.id, n]));
    this.maxWeight = data.links.reduce((m, l) => Math.max(m, l.weight), 0);
    this.adjacency = new Map();
    for (const l of data.links) {
      const s = endId(l.source);
      const t = endId(l.target);
      if (!this.adjacency.has(s)) this.adjacency.set(s, new Set());
      if (!this.adjacency.has(t)) this.adjacency.set(t, new Set());
      this.adjacency.get(s)?.add(t);
      this.adjacency.get(t)?.add(s);
    }
    if (key !== this.compKey) {
      this.compKey = key;
      this.fg.graphData({ nodes, links: data.links });
    } else {
      // Тот же состав: перекрасить и пересчитать размеры без перезапуска раскладки.
      this.fg.nodeColor(this.nodeColorFn()).linkColor(this.linkColorFn());
    }
    this.dirty = true;
  }

  /** Подсветка: наведённый узел с соседями, иначе пункт легенды, иначе выбранный с соседями. */
  private computeActive() {
    const around = (id: string | null) =>
      id !== null && this.nodes.has(id) ? new Set([id, ...(this.adjacency.get(id) ?? [])]) : null;
    const hovered = around(this.hovered);
    if (hovered) {
      this.active = hovered;
      this.center = this.hovered;
    } else if (this.emphasis) {
      this.active = new Set([...this.emphasis].filter((id) => this.nodes.has(id)));
      this.center = null;
    } else {
      this.active = around(this.selected);
      this.center = this.active ? this.selected : null;
    }
  }

  /** Пересчитать подсветку: цвета узлов и связей, подписи соседей. */
  private applyActive() {
    this.computeActive();
    this.fg.nodeColor(this.nodeColorFn()).linkColor(this.linkColorFn());
    this.updateLabels();
  }

  emphasize(ids: ReadonlySet<string> | null) {
    if (this.disposed) return;
    this.emphasis = ids;
    this.applyActive();
  }

  zoom(dir: 1 | -1) {
    if (this.disposed) return;
    this.tracking = null;
    const target = this.controls.target.clone();
    const dist = this.camera.position.distanceTo(target);
    const next = dir > 0 ? dist / ZOOM_FACTOR : dist * ZOOM_FACTOR;
    this.userMoved = true;
    this.flyTo(target, target.clone().add(this.viewDirection().multiplyScalar(next)), FIT_MS);
  }

  setPalette(palette: Palette) {
    if (this.disposed) return;
    const changed =
      palette.bg !== this.palette.bg ||
      palette.ink !== this.palette.ink ||
      palette.edge !== this.palette.edge ||
      palette.edgeActive !== this.palette.edgeActive;
    this.palette = palette;
    if (!changed) return;
    (this.scene.background as Color).setStyle(palette.bg);
    this.fg.nodeColor(this.nodeColorFn()).linkColor(this.linkColorFn());
    this.dirty = true;
  }

  /** Подписи и подсветка: топ по размеру, совпадения поиска, выбранный узел и окрестность. */
  setDecorations(highlight: string, selected: string | null) {
    if (this.disposed) return;
    this.highlight = highlight;
    this.selected = selected;
    this.applyActive();
  }

  private updateLabels() {
    const list = [...this.nodes.values()];
    const selected = this.selected;
    this.matches = highlightMatches(list, this.highlight);
    const topMatches = [...this.matches]
      .map((id) => this.nodes.get(id) as ForceNode)
      .sort((a, b) => b.val - a.val)
      .slice(0, MATCH_LABEL_LIMIT)
      .map((n) => n.id);
    const always = [...(selected ? [selected] : []), ...topMatches, ...(this.center ? (this.active ?? []) : [])];
    // При подсветке окрестности подписаны только она и совпадения: остальное приглушено.
    const wanted = labelCandidates(list, this.center ? 0 : LABEL_LIMIT, always);
    for (const [id, label] of this.labels) {
      if (!wanted.has(id)) {
        disposeLabel(label);
        this.labels.delete(id);
      }
    }
    for (const id of wanted) {
      const node = this.nodes.get(id);
      if (!node) continue;
      const strong = this.matches.has(id) || id === selected || id === this.center;
      const key = `${node.name}\u0000${this.palette.ink}\u0000${this.palette.bg}\u0000${strong ? 1 : 0}`;
      const old = this.labels.get(id);
      if (old?.key === key) continue;
      if (old) disposeLabel(old);
      const label = createLabel(node.name, this.palette, strong);
      label.key = key;
      this.labels.set(id, label);
      this.labelLayer.add(label.sprite);
    }
    this.applyNodeScales();
    this.dirty = true;
  }

  setAutoRotate(on: boolean) {
    if (this.disposed) return;
    this.controls.autoRotate = on && !this.reduced;
    this.dirty = true;
  }

  // ——— камера ———

  /** Вписать видимые узлы; `insets` — места под панели по краям: граф — в свободной части кадра. */
  fit(insets: FitInsets = NO_INSETS) {
    if (this.disposed) return;
    this.tracking = null;
    const sphere = boundingSphere(
      [...this.nodes.values()].map((n) => ({ x: n.x ?? NaN, y: n.y ?? NaN, z: n.z ?? NaN, r: nodeRadius(n) })),
    );
    if (!sphere) {
      this.flyTo(new Vector3(0, 0, 0), new Vector3(0, 0, 400), FIT_MS);
      return;
    }
    const center = new Vector3(sphere.center.x, sphere.center.y, sphere.center.z);
    const width = this.container.clientWidth;
    const height = this.container.clientHeight;
    const radius = Math.max(sphere.radius, 20);
    if (!(width > 0) || !(height > 0)) {
      const dist = fitDistance(radius, FOV, this.camera.aspect) * FIT_MARGIN;
      this.flyTo(center, center.clone().add(this.viewDirection().multiplyScalar(dist)), FIT_MS);
      return;
    }
    const pad = usableInsets(insets, width, height, 0);
    const w = width - pad.left - pad.right;
    const h = height - pad.top - pad.bottom;
    // Вертикальный угол обзора свободной части кадра: та же камера, но видна доля h / height.
    const tanHalf = Math.tan(((FOV / 2) * Math.PI) / 180);
    const freeFov = ((2 * Math.atan((tanHalf * h) / height)) * 180) / Math.PI;
    const dist = fitDistance(radius, freeFov, w / h) * FIT_MARGIN;
    // Центр графа — в центр свободного прямоугольника (как сдвиг в focusPose).
    const perPx = (2 * dist * tanHalf) / height;
    this.camera.updateMatrixWorld();
    const right = new Vector3().setFromMatrixColumn(this.camera.matrixWorld, 0).normalize();
    const up = new Vector3().setFromMatrixColumn(this.camera.matrixWorld, 1).normalize();
    const target = center
      .clone()
      .addScaledVector(right, (-(pad.left - pad.right) / 2) * perPx)
      .addScaledVector(up, ((pad.top - pad.bottom) / 2) * perPx);
    this.flyTo(target, target.clone().add(this.viewDirection().multiplyScalar(dist)), FIT_MS);
  }

  /**
   * Перелёт к узлу. `offset` (CSS-пиксели) сдвигает узел от центра кадра: камера смотрит
   * правее/ниже узла, и он оказывается левее/выше — например, не под карточкой сущности.
   * У узла ещё нет позиции (сцена после смены вида) или симуляция его двигает — камера
   * встаёт на него, как только он появится, и следует за ним до остановки движка.
   */
  focusNode(id: string, offset: FocusOffset = { x: 0, y: 0 }) {
    // Узла нет среди видимых — ничего: иначе userMoved и слежение за пропавшим узлом
    // отключили бы автокадрирование этой сцены.
    if (this.disposed || !this.nodes.has(id)) return;
    this.userMoved = true;
    const pose = this.focusPose(id, offset);
    if (!pose || this.engineRunning) {
      this.tracking = { id, offset, placed: pose !== null };
      this.lastTrack = performance.now();
      if (pose) this.flyTo(pose.target, pose.position, FLY_MS);
      return;
    }
    this.tracking = null;
    this.flyTo(pose.target, pose.position, FLY_MS);
  }

  /** Куда смотреть и где стоять камере, чтобы в свободной части кадра была вся окрестность узла. */
  private focusPose(id: string, offset: FocusOffset): { target: Vector3; position: Vector3 } | null {
    const node = this.nodes.get(id);
    if (!node || node.x === undefined || node.y === undefined || node.z === undefined) return null;
    // Камера смотрит на сам узел, а не на центр окрестности: радиус кадра — до дальнего соседа.
    let reach = 0;
    for (const nid of this.adjacency.get(id) ?? []) {
      const n = this.nodes.get(nid);
      if (!n || n.x === undefined || n.y === undefined || n.z === undefined) continue;
      reach = Math.max(reach, Math.hypot(n.x - node.x, n.y - node.y, n.z - node.z) + nodeRadius(n));
    }
    const w = Math.max(1, this.container.clientWidth - Math.abs(offset.x) * 2);
    const h = Math.max(1, this.container.clientHeight);
    const spread = Math.max(reach, nodeRadius(node) * 4, 25);
    const dist = fitDistance(spread, FOV, w / Math.max(1, h - Math.abs(offset.y) * 2)) * 1.05;
    const perPx = (2 * dist * Math.tan(((FOV / 2) * Math.PI) / 180)) / h;
    this.camera.updateMatrixWorld();
    const right = new Vector3().setFromMatrixColumn(this.camera.matrixWorld, 0).normalize();
    const up = new Vector3().setFromMatrixColumn(this.camera.matrixWorld, 1).normalize();
    const target = new Vector3(node.x, node.y, node.z)
      .addScaledVector(right, offset.x * perPx)
      .addScaledVector(up, -offset.y * perPx);
    return { target, position: target.clone().add(this.viewDirection().multiplyScalar(dist)) };
  }

  /** Шаг слежения за выбранным узлом (каждый кадр цикла, пока `tracking` не снят). */
  private track(now: number) {
    const t = this.tracking;
    if (!t) return;
    if (!this.nodes.has(t.id)) {
      this.tracking = null;
      return;
    }
    const pose = this.focusPose(t.id, t.offset);
    if (!pose) return;
    if (!t.placed) {
      // Позиция только появилась: сразу на узел (при reduced motion — без анимации).
      t.placed = true;
      this.lastTrack = now;
      this.flyTo(pose.target, pose.position, FLY_MS);
      if (!this.engineRunning) this.tracking = null;
      return;
    }
    if (this.flight) {
      // Перелёт ещё идёт — ведём его к текущему месту узла, а не к прежнему.
      this.flight.toTarget.copy(pose.target);
      this.flight.toPos.copy(pose.position);
      this.lastTrack = now;
      return;
    }
    if (!this.engineRunning) {
      this.tracking = null;
      this.flyTo(pose.target, pose.position, FIT_MS);
      return;
    }
    const k = this.reduced ? 1 : 1 - Math.exp(-Math.min(64, now - this.lastTrack) / FOLLOW_TAU_MS);
    this.lastTrack = now;
    this.controls.target.lerp(pose.target, k);
    this.camera.position.lerp(pose.position, k);
    this.dirty = true;
  }

  private viewDirection(): Vector3 {
    const dir = this.camera.position.clone().sub(this.controls.target);
    return dir.lengthSq() > 1e-9 ? dir.normalize() : new Vector3(0, 0, 1);
  }

  private flyTo(target: Vector3, position: Vector3, duration: number) {
    if (this.reduced) {
      this.flight = null;
      this.controls.target.copy(target);
      this.camera.position.copy(position);
      this.controls.update();
      this.dirty = true;
      return;
    }
    this.flight = {
      fromPos: this.camera.position.clone(),
      fromTarget: this.controls.target.clone(),
      toPos: position,
      toTarget: target,
      start: performance.now(),
      duration,
    };
  }

  // ——— экспорт и освобождение ———

  exportPng(): Promise<Blob> {
    return new Promise((resolve, reject) => {
      if (this.disposed) {
        reject(new Error("Граф уже закрыт"));
        return;
      }
      // Буфер WebGL не сохраняется между кадрами: рисуем и сразу снимаем (toBlob копирует синхронно).
      this.render();
      this.renderer.domElement.toBlob(
        (blob) => (blob ? resolve(blob) : reject(new Error("Не удалось получить PNG"))),
        "image/png",
      );
    });
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    cancelAnimationFrame(this.frame);
    this.resizer?.disconnect();
    const canvas = this.renderer.domElement;
    canvas.removeEventListener("pointermove", this.onPointerMove);
    canvas.removeEventListener("pointerleave", this.onPointerLeave);
    canvas.removeEventListener("pointerdown", this.onPointerDown);
    canvas.removeEventListener("pointerup", this.onPointerUp);
    canvas.removeEventListener("pointercancel", this.onPointerCancel);
    this.controls.removeEventListener("change", this.onControlsChange);
    this.controls.removeEventListener("start", this.onControlsStart);
    this.controls.dispose();
    for (const label of this.labels.values()) disposeLabel(label);
    this.labels.clear();
    // Геометрии и материалы у three-forcegraph общие между узлами — собираем уникальные.
    const geometries = new Set<BufferGeometry>();
    const materials = new Set<Material>();
    const textures = new Set<Texture>();
    this.scene.traverse((obj) => {
      const o = obj as Object3D & { geometry?: BufferGeometry; material?: Material | Material[] };
      if (o.geometry) geometries.add(o.geometry);
      for (const m of Array.isArray(o.material) ? o.material : o.material ? [o.material] : []) {
        materials.add(m);
        const map = (m as Material & { map?: Texture | null }).map;
        if (map) textures.add(map);
      }
    });
    geometries.forEach((g) => g.dispose());
    materials.forEach((m) => m.dispose());
    textures.forEach((t) => t.dispose());
    this.scene.clear();
    this.renderer.dispose();
    this.renderer.forceContextLoss();
    canvas.remove();
    this.nodes.clear();
    this.hovered = null;
    this.flight = null;
    this.tracking = null;
  }

  // ——— цикл ———

  private readonly loop = (now: number) => {
    if (this.disposed) return;
    this.frame = requestAnimationFrame(this.loop);
    this.fg.tickFrame();
    this.track(now);
    if (this.flight) {
      const f = this.flight;
      const t = easeInOutCubic((now - f.start) / f.duration);
      this.camera.position.lerpVectors(f.fromPos, f.toPos, t);
      this.controls.target.lerpVectors(f.fromTarget, f.toTarget, t);
      if (t >= 1) this.flight = null;
      this.dirty = true;
    }
    // update() сам шлёт "change", если камера сдвинулась (затухание, автоповорот).
    this.controls.update();
    if (!this.dirty && !this.engineRunning) return;
    this.dirty = false;
    this.updateHover();
    this.render();
  };

  private render() {
    this.placeLabels();
    this.renderer.render(this.scene, this.camera);
  }

  private resize() {
    if (this.disposed) return;
    const w = Math.max(1, this.container.clientWidth);
    const h = Math.max(1, this.container.clientHeight);

    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    // Спрайт без sizeAttenuation: масштаб 1 ≈ высота кадра / (2·tan(fov/2)) пикселей.
    this.labelUnit = (2 * Math.tan(((FOV / 2) * Math.PI) / 180)) / h;
    this.dirty = true;
    // Кадр сразу, иначе при перетаскивании разделителя мелькает растянутый холст.
    this.render();
  }

  /** Подписи над узлами: смещение вверх по экрану на радиус, постоянный размер в пикселях. */
  private placeLabels() {
    if (this.labels.size === 0) return;
    this.camera.updateMatrixWorld();
    this.up.setFromMatrixColumn(this.camera.matrixWorld, 1).normalize();
    for (const [id, label] of this.labels) {
      const node = this.nodes.get(id) as BoundNode | undefined;
      const obj = node?.__threeObj;
      if (!node || !obj || node.x === undefined) {
        label.sprite.visible = false;
        continue;
      }
      label.sprite.visible = true;
      const lift = nodeRadius(node) * obj.scale.x * 1.1 + 1;
      label.sprite.position.copy(obj.position).addScaledVector(this.up, lift);
      const hgt = (LABEL_PX + 4) * this.labelUnit;
      label.sprite.scale.set(hgt * label.aspect, hgt, 1);
    }
  }

  private applyNodeScales() {
    for (const node of this.nodes.values()) {
      const obj = (node as BoundNode).__threeObj;
      if (!obj) continue;
      const s = (this.matches.has(node.id) ? MATCH_SCALE : 1) * (node.id === this.hovered ? HOVER_SCALE : 1);
      obj.scale.setScalar(s);
    }
  }

  // ——— наведение и выбор ———

  private pick(clientX: number, clientY: number): ForceNode | null {
    const rect = this.renderer.domElement.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return null;
    this.ndc.set(((clientX - rect.left) / rect.width) * 2 - 1, -((clientY - rect.top) / rect.height) * 2 + 1);
    this.raycaster.setFromCamera(this.ndc, this.camera);
    const targets = this.fg.children.filter((o) => (o as NodeObj).__graphObjType === "node" && o.visible);
    for (const hit of this.raycaster.intersectObjects(targets, false)) {
      const data = (hit.object as NodeObj).__data;
      if (data && this.nodes.get(data.id) === data) return data;
    }
    return null;
  }

  private updateHover() {
    const p = this.pointer;
    if (!p.inside || this.press) {
      this.setHovered(null);
      return;
    }
    // Вызывается только на отрисованном кадре: сдвинулся курсор, камера (перелёт,
    // затухание, автоповорот) или узлы — под курсором мог оказаться другой узел.
    this.setHovered(this.pick(p.x, p.y));
  }

  private setHovered(node: ForceNode | null) {
    const id = node?.id ?? null;
    if (id === this.hovered) return;
    this.hovered = id;
    this.renderer.domElement.style.cursor = id ? "pointer" : "";
    this.applyActive();
    this.applyNodeScales();
    this.callbacks.onHover(node);
    this.dirty = true;
  }

  /** Подсказка у курсора: позиция через CSSOM (style-атрибуты CSP запрещает, CSSOM — нет). */
  placeTooltip() {
    placeTooltip(this.callbacks.tooltip(), this.container, this.pointer.x, this.pointer.y);
  }

  private readonly onPointerMove = (e: PointerEvent) => {
    this.pointer.x = e.clientX;
    this.pointer.y = e.clientY;
    this.pointer.inside = true;
    this.dirty = true;
    if (this.hovered) this.placeTooltip();
  };

  private readonly onPointerLeave = () => {
    this.pointer.inside = false;
    this.press = null;
    this.setHovered(null);
  };

  private readonly onPointerDown = (e: PointerEvent) => {
    if (e.button !== 0) return;
    this.press = { x: e.clientX, y: e.clientY, id: e.pointerId };
  };

  private readonly onPointerUp = (e: PointerEvent) => {
    const press = this.press;
    this.press = null;
    this.dirty = true;
    if (!press || press.id !== e.pointerId) return;
    if (Math.hypot(e.clientX - press.x, e.clientY - press.y) > CLICK_SLOP_PX) return;
    const node = this.pick(e.clientX, e.clientY);
    if (!node) {
      this.lastClick = null;
      this.callbacks.onBackground();
      return;
    }
    const now = performance.now();
    const last = this.lastClick;
    if (last && last.id === node.id && now - last.time <= DOUBLE_CLICK_MS) {
      // Второй клик по тому же узлу: он уже выбран первым, теперь — раскрыть соседей.
      this.lastClick = null;
      this.callbacks.onExpand(node.id);
      return;
    }
    this.lastClick = { id: node.id, time: now };
    // Перелёт к выбранному узлу делает страница (focusNode с местом под карточку).
    this.callbacks.onSelect(node.id);
  };

  private readonly onPointerCancel = () => {
    this.press = null;
  };

  private readonly onControlsChange = () => {
    this.dirty = true;
  };

  private readonly onControlsStart = () => {
    this.userMoved = true;
    this.flight = null;
    this.tracking = null;
  };

  // ——— события three-forcegraph ———

  /** Новая функция при каждом вызове: three-forcegraph перекрашивает только при смене аксессора. */
  private nodeColorFn() {
    const active = this.active;
    const bg = this.palette.bg;
    return (n: ForceNode) => (active === null || active.has(n.id) ? n.color : mixRgb(n.color, bg, DIM_MIX));
  }

  private linkColorFn() {
    const { active, center, palette, maxWeight } = this;
    return (l: ForceLink) => {
      const s = endId(l.source);
      const t = endId(l.target);
      if (center !== null && (s === center || t === center)) return withAlpha(palette.edgeActive, 0.95);
      if (active !== null) return withAlpha(palette.edge, 0.06);
      return withAlpha(palette.edge, linkAlpha(l.weight, maxWeight));
    };
  }

  private readonly onEngineTick = () => {
    this.engineRunning = true;
    this.dirty = true;
    // Пока раскладка расходится и камеру никто не трогал — кадр плавно следует за графом.
    if (!this.userMoved && !this.flight) this.followGraph(0.08);
  };

  private readonly onEngineStop = () => {
    if (this.disposed) return;
    this.engineRunning = false;
    this.dirty = true;
    this.callbacks.onSettle();
    if (!this.userMoved) this.followGraph(1);
  };

  private readonly onFinishUpdate = () => {
    if (this.disposed) return;
    // Новые меши узлов созданы — вернуть им масштаб подсветки.
    this.applyNodeScales();
    this.dirty = true;
  };

  private followGraph(k: number) {
    const sphere = boundingSphere(
      [...this.nodes.values()].map((n) => ({ x: n.x ?? NaN, y: n.y ?? NaN, z: n.z ?? NaN, r: nodeRadius(n) })),
    );
    if (!sphere) return;
    this.tmp.set(sphere.center.x, sphere.center.y, sphere.center.z);
    const dist = fitDistance(Math.max(sphere.radius, 20), FOV, this.camera.aspect) * FIT_MARGIN;
    const dir = this.viewDirection();
    this.controls.target.lerp(this.tmp, k);
    const current = this.camera.position.distanceTo(this.controls.target);
    this.camera.position.copy(this.controls.target).addScaledVector(dir, current + (dist - current) * k);
  }
}

/** Интерактивный 3D-граф (three.js + three-forcegraph): вращение, выбор, перелёт к узлу. */
export function Graph3D({
  graph,
  filters,
  entityTypes,
  typeLabel,
  selected = null,
  onSelect,
  onBackgroundClick,
  onReady,
  onExpand,
  autoRotate = false,
}: Graph3DProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const tooltipRef = useRef<HTMLDivElement>(null);
  const sceneRef = useRef<Scene3D | null>(null);
  const onSelectRef = useRef(onSelect);
  const onBackgroundRef = useRef(onBackgroundClick);
  const onReadyRef = useRef(onReady);
  const onExpandRef = useRef(onExpand);
  const graphRef = useRef(graph);
  /** Позиции узлов прежней сцены: новая сцена (раскрытие, смена режима) стартует с них. */
  const positionsRef = useRef<ReadonlyMap<string, SeedPosition>>(new Map());
  const typeLabelRef = useRef(typeLabel);
  const [hover, setHover] = useState<TooltipInfo | null>(null);
  const [unsupported, setUnsupported] = useState(false);
  const [themeVersion, setThemeVersion] = useState(0);
  // Подписи рисуются на canvas: шрифт нужен до первой подписи.
  const [fontsLoaded, setFontsLoaded] = useState(() => fontsReady(LABEL_FONTS));

  useEffect(() => {
    onSelectRef.current = onSelect;
    onBackgroundRef.current = onBackgroundClick;
    onReadyRef.current = onReady;
    onExpandRef.current = onExpand;
    graphRef.current = graph;
    typeLabelRef.current = typeLabel;
  });

  useEffect(() => {
    if (fontsLoaded) return;
    let cancelled = false;
    void loadFonts(LABEL_FONTS).then(() => {
      if (!cancelled) setFontsLoaded(true);
    });
    return () => {
      cancelled = true;
    };
  }, [fontsLoaded]);

  // Сцена на каждый новый граф; разрушение при размонтировании.
  useEffect(() => {
    const container = containerRef.current;
    if (!container || !fontsLoaded) return;
    const toTooltip = (node: ForceNode | null): TooltipInfo | null => {
      const g = graphRef.current;
      if (!node || !g.hasNode(node.id)) return null;
      const a = g.getNodeAttributes(node.id);
      const type = typeLabelRef.current?.(a.type) ?? a.type;
      return { id: node.id, name: a.name, type, mentions: a.mention_count, color: node.color };
    };
    let scene: Scene3D;
    const remember = () => {
      positionsRef.current = scene.positions();
    };
    try {
      scene = new Scene3D(container, {
        onSelect: (id) => onSelectRef.current(id),
        onExpand: (id) => onExpandRef.current?.(id),
        onSettle: () => remember(),
        onBackground: () => onBackgroundRef.current?.(),
        onHover: (node) => setHover(toTooltip(node)),
        tooltip: () => tooltipRef.current,
      });
    } catch {
      // Нет WebGL (или контекст не создан) — показываем сообщение вместо холста.
      setUnsupported(true);
      return;
    }
    setUnsupported(false);
    scene.setSeeds(seedPositions(graph, positionsRef.current));
    sceneRef.current = scene;

    const bump = () => setThemeVersion((v) => v + 1);
    const observer = new MutationObserver(bump);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    const scheme = typeof matchMedia === "function" ? matchMedia("(prefers-color-scheme: dark)") : null;
    scheme?.addEventListener("change", bump);

    let disposed = false;
    const dispose = () => {
      if (disposed) return;
      disposed = true;
      remember();
      observer.disconnect();
      scheme?.removeEventListener("change", bump);
      scene.dispose();
      if (sceneRef.current === scene) sceneRef.current = null;
      setHover(null);
    };
    onReadyRef.current?.({
      exportPng: () => scene.exportPng(),
      fit: (insets) => scene.fit(insets),
      dispose,
      zoomIn: () => scene.zoom(1),
      zoomOut: () => scene.zoom(-1),
      focusNode: (id, offset) => scene.focusNode(id, offset),
      emphasize: (ids) => scene.emphasize(ids),
    });
    return dispose;
  }, [graph, fontsLoaded]);

  // Состав, цвета и размеры. Тот же состав — только перекраска, раскладка не перезапускается.
  const typesKey = filters.types.join("\u0000");
  const predicatesKey = filters.predicates.join("\u0000");
  const typesOrder = (entityTypes ?? []).join("\u0000");
  const { community, minWeight, minMentions, hideIsolated, colorBy, sizeBy } = filters;
  useEffect(() => {
    const scene = sceneRef.current;
    if (!scene) return;
    const visible = visibleSubgraph(graph, {
      ...DEFAULT_FILTERS,
      types: typesKey === "" ? [] : typesKey.split("\u0000"),
      community,
      minWeight,
      minMentions,
      hideIsolated,
      predicates: predicatesKey === "" ? [] : predicatesKey.split("\u0000"),
    });
    scene.setPalette(readPalette());
    scene.setData(
      toForceData(graph, visible, { colorBy, sizeBy }, typesOrder === "" ? undefined : typesOrder.split("\u0000")),
    );
  }, [
    graph,
    fontsLoaded,
    typesKey,
    predicatesKey,
    community,
    minWeight,
    minMentions,
    hideIsolated,
    colorBy,
    sizeBy,
    typesOrder,
    themeVersion,
  ]);

  // Подписи и подсветка поиска; зависят от состава и размеров, поэтому идут после данных.
  const highlight = filters.highlight;
  useEffect(() => {
    sceneRef.current?.setDecorations(highlight, selected);
  }, [
    graph,
    fontsLoaded,
    highlight,
    selected,
    typesKey,
    predicatesKey,
    community,
    minWeight,
    minMentions,
    hideIsolated,
    colorBy,
    sizeBy,
    typesOrder,
    themeVersion,
  ]);

  useEffect(() => {
    sceneRef.current?.setAutoRotate(autoRotate);
  }, [graph, fontsLoaded, autoRotate]);

  // Подсказка появилась — сразу поставить её к курсору, без кадра в углу.
  useLayoutEffect(() => {
    if (hover) sceneRef.current?.placeTooltip();
  }, [hover]);

  if (unsupported) {
    return (
      <div className="grid h-full min-h-0 w-full place-items-center p-6 text-center text-sm text-muted">
        3D-вид недоступен: браузер не поддерживает WebGL.
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className="relative h-full min-h-0 w-full overflow-hidden"
      role="img"
      aria-label="Трёхмерный граф связей между сущностями"
    >
      {hover && <GraphTooltip ref={tooltipRef} info={hover} />}
    </div>
  );
}

export default Graph3D;

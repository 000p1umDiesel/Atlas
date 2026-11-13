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
import { cssColor, withAlpha } from "../lib/entityColors";
import type { Graph2DApi, Graph2DProps } from "./Graph2D";
import { DEFAULT_FILTERS, visibleSubgraph } from "./model";
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

/** Мировой радиус узла на единицу `nodeSize` (4…22 → 2…11). */
const NODE_SCALE = 0.5;
const FOV = 50;
const LABEL_LIMIT = 30;
/** Сколько совпадений поиска подписывать сверх топа (крупнейшие). */
const MATCH_LABEL_LIMIT = 60;
const MATCH_SCALE = 1.35;
const HOVER_SCALE = 1.15;
const FLY_MS = 600;
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
}

function readPalette(): Palette {
  return { bg: cssColor("--bg"), ink: cssColor("--ink"), edge: cssColor("--muted") };
}

function prefersReducedMotion(): boolean {
  return typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
}

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
  const fontPx = 12 * LABEL_DENSITY;
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
  onHover(node: { id: string; name: string } | null): void;
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
  private flight: Flight | null = null;
  /** Камеру трогал пользователь: автокадрирование больше не вмешивается. */
  private userMoved = false;
  private engineRunning = false;
  private dirty = true;
  private frame = 0;
  private labelUnit = 0;
  private width = 1;
  private pointer = { x: 0, y: 0, inside: false };
  private press: { x: number; y: number; id: number } | null = null;
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
    this.controls.dampingFactor = 0.12;
    this.controls.autoRotateSpeed = 0.8;
    this.controls.addEventListener("change", this.onControlsChange);
    this.controls.addEventListener("start", this.onControlsStart);

    this.fg = new ThreeForceGraph<ForceNode, ForceLink>()
      .nodeId("id")
      .nodeRelSize(1)
      // three-forcegraph берёт радиус как cbrt(val) · relSize — передаём куб радиуса.
      .nodeVal((n) => Math.pow(nodeRadius(n), 3))
      .nodeColor("color")
      .nodeOpacity(0.95)
      .nodeResolution(14)
      .linkWidth(0)
      .linkOpacity(1)
      .linkColor(this.linkColor)
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

  setData(data: ForceData) {
    if (this.disposed) return;
    const key = compositionKey(data);
    // Был пустой кадр — новый состав снова кадрируется автоматически.
    if (this.nodes.size === 0) this.userMoved = false;
    const nodes = reconcileNodes(this.nodes, data.nodes);
    this.nodes = new Map(nodes.map((n) => [n.id, n]));
    this.maxWeight = data.links.reduce((m, l) => Math.max(m, l.weight), 0);
    if (key !== this.compKey) {
      this.compKey = key;
      this.fg.graphData({ nodes, links: data.links });
    } else {
      // Тот же состав: перекрасить и пересчитать размеры без перезапуска раскладки.
      this.fg.nodeColor("color").linkColor(this.linkColor);
    }
    this.dirty = true;
  }

  setPalette(palette: Palette) {
    if (this.disposed) return;
    const changed =
      palette.bg !== this.palette.bg || palette.ink !== this.palette.ink || palette.edge !== this.palette.edge;
    this.palette = palette;
    if (!changed) return;
    (this.scene.background as Color).setStyle(palette.bg);
    this.fg.linkColor(this.linkColor);
    this.dirty = true;
  }

  /** Подписи и подсветка: топ по размеру, совпадения поиска и выбранный узел. */
  setDecorations(highlight: string, selected: string | null) {
    if (this.disposed) return;
    const list = [...this.nodes.values()];
    this.matches = highlightMatches(list, highlight);
    const topMatches = [...this.matches]
      .map((id) => this.nodes.get(id) as ForceNode)
      .sort((a, b) => b.val - a.val)
      .slice(0, MATCH_LABEL_LIMIT)
      .map((n) => n.id);
    const wanted = labelCandidates(list, LABEL_LIMIT, selected ? [selected, ...topMatches] : topMatches);
    for (const [id, label] of this.labels) {
      if (!wanted.has(id)) {
        disposeLabel(label);
        this.labels.delete(id);
      }
    }
    for (const id of wanted) {
      const node = this.nodes.get(id);
      if (!node) continue;
      const strong = this.matches.has(id) || id === selected;
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

  fit() {
    if (this.disposed) return;
    const sphere = boundingSphere(
      [...this.nodes.values()].map((n) => ({ x: n.x ?? NaN, y: n.y ?? NaN, z: n.z ?? NaN, r: nodeRadius(n) })),
    );
    if (!sphere) {
      this.flyTo(new Vector3(0, 0, 0), new Vector3(0, 0, 400), FIT_MS);
      return;
    }
    const target = new Vector3(sphere.center.x, sphere.center.y, sphere.center.z);
    const dist = fitDistance(Math.max(sphere.radius, 20), FOV, this.camera.aspect) * FIT_MARGIN;
    this.flyTo(target, target.clone().add(this.viewDirection().multiplyScalar(dist)), FIT_MS);
  }

  private flyToNode(node: ForceNode) {
    if (node.x === undefined || node.y === undefined || node.z === undefined) return;
    const target = new Vector3(node.x, node.y, node.z);
    const current = this.camera.position.distanceTo(this.controls.target);
    const dist = Math.min(current, Math.max(nodeRadius(node) * 10, 80));
    this.flyTo(target, target.clone().add(this.viewDirection().multiplyScalar(dist)), FLY_MS);
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
  }

  // ——— цикл ———

  private readonly loop = (now: number) => {
    if (this.disposed) return;
    this.frame = requestAnimationFrame(this.loop);
    this.fg.tickFrame();
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
    this.width = w;
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
    this.applyNodeScales();
    this.callbacks.onHover(node ? { id: node.id, name: node.name } : null);
    this.dirty = true;
  }

  /** Подсказка у курсора: позиция через CSSOM (style-атрибуты CSP запрещает, CSSOM — нет). */
  placeTooltip() {
    const el = this.callbacks.tooltip();
    if (!el) return;
    const rect = this.container.getBoundingClientRect();
    const x = Math.min(this.pointer.x - rect.left + 14, Math.max(0, this.width - el.offsetWidth - 4));
    const y = Math.max(0, this.pointer.y - rect.top - el.offsetHeight - 10);
    el.style.transform = `translate(${Math.round(x)}px, ${Math.round(y)}px)`;
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
    if (!node) return;
    this.callbacks.onSelect(node.id);
    this.userMoved = true;
    this.flyToNode(node);
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
  };

  // ——— события three-forcegraph ———

  private readonly linkColor = (l: ForceLink) => withAlpha(this.palette.edge, linkAlpha(l.weight, this.maxWeight));

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
  selected = null,
  onSelect,
  onReady,
  autoRotate = false,
}: Graph3DProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const tooltipRef = useRef<HTMLDivElement>(null);
  const sceneRef = useRef<Scene3D | null>(null);
  const onSelectRef = useRef(onSelect);
  const onReadyRef = useRef(onReady);
  const [hover, setHover] = useState<{ id: string; name: string } | null>(null);
  const [unsupported, setUnsupported] = useState(false);
  const [themeVersion, setThemeVersion] = useState(0);

  useEffect(() => {
    onSelectRef.current = onSelect;
    onReadyRef.current = onReady;
  });

  // Сцена на каждый новый граф; разрушение при размонтировании.
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    let scene: Scene3D;
    try {
      scene = new Scene3D(container, {
        onSelect: (id) => onSelectRef.current(id),
        onHover: setHover,
        tooltip: () => tooltipRef.current,
      });
    } catch {
      // Нет WebGL (или контекст не создан) — показываем сообщение вместо холста.
      setUnsupported(true);
      return;
    }
    setUnsupported(false);
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
      observer.disconnect();
      scheme?.removeEventListener("change", bump);
      scene.dispose();
      if (sceneRef.current === scene) sceneRef.current = null;
      setHover(null);
    };
    onReadyRef.current?.({
      exportPng: () => scene.exportPng(),
      fit: () => scene.fit(),
      dispose,
    });
    return dispose;
  }, [graph]);

  // Состав, цвета и размеры. Тот же состав — только перекраска, раскладка не перезапускается.
  const typesKey = filters.types.join("\u0000");
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
    });
    scene.setPalette(readPalette());
    scene.setData(
      toForceData(graph, visible, { colorBy, sizeBy }, typesOrder === "" ? undefined : typesOrder.split("\u0000")),
    );
  }, [graph, typesKey, community, minWeight, minMentions, hideIsolated, colorBy, sizeBy, typesOrder, themeVersion]);

  // Подписи и подсветка поиска; зависят от состава и размеров, поэтому идут после данных.
  const highlight = filters.highlight;
  useEffect(() => {
    sceneRef.current?.setDecorations(highlight, selected);
  }, [
    graph,
    highlight,
    selected,
    typesKey,
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
  }, [graph, autoRotate]);

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
      {hover && (
        <div
          ref={tooltipRef}
          className="pointer-events-none absolute left-0 top-0 z-10 max-w-xs truncate rounded-lg bg-card px-2 py-1 text-xs text-ink shadow-card"
        >
          {hover.name}
        </div>
      )}
    </div>
  );
}

export default Graph3D;

// Горячие клавиши графа: чистое сопоставление «клавиша → действие» и поиск первого совпадения.
import type { AbstractGraph } from "graphology-types";

export type GraphAction = "fit" | "view2d" | "view3d" | "search" | "escape" | "help" | "zoomIn" | "zoomOut";

/** Нужное от KeyboardEvent (в тестах — простой объект). */
export interface KeyLike {
  code: string;
  key: string;
  shiftKey: boolean;
  ctrlKey: boolean;
  metaKey: boolean;
  altKey: boolean;
  target: EventTarget | null;
  /** Событие уже обработал кто-то другой (Radix-список, меню). */
  defaultPrevented?: boolean;
}

/** Атрибут карточки сущности графа: это наш немодальный диалог, клавиши в нём работают. */
export const GRAPH_CARD_ATTR = "data-graph-card";

/** Внутри всплывающего списка, меню или чужого диалога клавиши принадлежат им. */
function insidePopup(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false;
  const popup = target.closest('[role="listbox"],[role="menu"],[role="dialog"]');
  return popup !== null && !popup.hasAttribute(GRAPH_CARD_ATTR);
}

/** Поля, где клавиши заняты вводом: текст, список, ползунок. */
function typing(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable || target.getAttribute("contenteditable") === "true") return true;
  return target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement;
}

/**
 * Действие для нажатия или null. Клавиши — по физическому коду (`KeyF`, `Digit2`, `Slash`),
 * поэтому работают и в русской раскладке. С Ctrl/Cmd/Alt не срабатывают; при вводе текста —
 * только Esc; во всплывающих списках, меню и чужих диалогах и для уже обработанных событий —
 * никакие.
 */
export function graphHotkey(e: KeyLike): GraphAction | null {
  if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return null;
  if (insidePopup(e.target)) return null;
  if (e.code === "Escape" || e.key === "Escape") return "escape";
  if (typing(e.target)) return null;
  if (e.key === "?") return "help";
  switch (e.code) {
    case "KeyF":
      return "fit";
    case "Digit2":
    case "Numpad2":
      return "view2d";
    case "Digit3":
    case "Numpad3":
      return "view3d";
    case "Slash":
      return e.shiftKey ? "help" : "search";
    case "Equal":
    case "NumpadAdd":
      return "zoomIn";
    case "Minus":
    case "NumpadSubtract":
      return "zoomOut";
    default:
      return null;
  }
}

/**
 * Первое совпадение подсветки: среди видимых узлов с подстрокой в имени (без учёта регистра) —
 * самое важное по PageRank (без анализа — по упоминаниям). Пустой запрос — null.
 */
export function firstMatch(
  graph: AbstractGraph,
  visible: ReadonlySet<string>,
  query: string,
): string | null {
  const q = query.trim().toLowerCase();
  if (q === "") return null;
  let best: string | null = null;
  let bestScore = -Infinity;
  for (const id of visible) {
    if (!graph.hasNode(id)) continue;
    const a = graph.getNodeAttributes(id) as { name: string; pagerank: number | null; mention_count: number };
    if (!a.name.toLowerCase().includes(q)) continue;
    const score = a.pagerank ?? a.mention_count / 1e6;
    if (score > bestScore) {
      best = id;
      bestScore = score;
    }
  }
  return best;
}

// Команды между колонкой фильтров и холстом графа (они в разных ветках дерева React).
const bus = new EventTarget();
const FLY = "fly-to-match";

/** Enter в поле «Подсветить по имени»: перелететь к первому совпадению и выбрать его. */
export function requestFlyToMatch(query: string): void {
  bus.dispatchEvent(new CustomEvent<string>(FLY, { detail: query }));
}

export function onFlyToMatch(listener: (query: string) => void): () => void {
  const handler = (e: Event) => listener((e as CustomEvent<string>).detail);
  bus.addEventListener(FLY, handler);
  return () => bus.removeEventListener(FLY, handler);
}

/** Атрибут поля подсветки: по `/` фокус уходит в видимое поле (их два — колонка и меню). */
export const HIGHLIGHT_FIELD_ATTR = "data-graph-highlight";

export function focusHighlightField(): boolean {
  const fields = document.querySelectorAll<HTMLInputElement>(`[${HIGHLIGHT_FIELD_ATTR}]`);
  const visible = [...fields].find((el) => el.offsetParent !== null || el.getClientRects().length > 0);
  if (!visible) return false;
  visible.focus();
  visible.select();
  return true;
}

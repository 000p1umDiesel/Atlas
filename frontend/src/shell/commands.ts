// Действия палитры Ctrl+K: переходы по разделам и смена темы; поиск по подписи и ключевым словам.
import type { ThemeChoice } from "../lib/theme";

export interface PaletteAction {
  id: string;
  label: string;
  /** Дополнительные слова для поиска (через пробел). */
  keywords: string;
  run: "navigate" | "theme";
  /** Путь для `navigate`. */
  to?: string;
}

export const ACTIONS: readonly PaletteAction[] = [
  { id: "new-chat", label: "Новый чат", keywords: "диалог вопрос спросить new chat", run: "navigate", to: "/chat" },
  { id: "upload", label: "Загрузить документ", keywords: "файл добавить upload", run: "navigate", to: "/documents" },
  { id: "compare", label: "Сравнить сущности", keywords: "сравнение две общие связи compare", run: "navigate", to: "/entities/compare" },
  { id: "go-home", label: "Перейти: Главная", keywords: "домой сводка home", run: "navigate", to: "/" },
  { id: "go-chat", label: "Перейти: Чат", keywords: "диалоги вопросы chat", run: "navigate", to: "/chat" },
  { id: "go-documents", label: "Перейти: Документы", keywords: "файлы загрузки documents", run: "navigate", to: "/documents" },
  { id: "go-entities", label: "Перейти: Сущности", keywords: "каталог entities", run: "navigate", to: "/entities" },
  { id: "go-graph", label: "Перейти: Граф", keywords: "связи сообщества graph", run: "navigate", to: "/graph" },
  { id: "go-wiki", label: "Перейти: Wiki", keywords: "вики статьи", run: "navigate", to: "/wiki" },
  { id: "go-status", label: "Перейти: Статус", keywords: "состояние воркер очередь status", run: "navigate", to: "/status" },
  { id: "theme", label: "Сменить тему", keywords: "тёмная светлая оформление dark light theme", run: "theme" },
];

/** Для сравнения: нижний регистр, `ё` = `е`. */
function fold(text: string): string {
  return text.toLowerCase().replaceAll("ё", "е");
}

/** Действия, где каждое слово запроса есть в подписи или ключевых словах; пустой запрос — все. */
export function filterActions(query: string, actions: readonly PaletteAction[] = ACTIONS): PaletteAction[] {
  const words = fold(query).split(/\s+/).filter(Boolean);
  return actions.filter((action) => {
    const haystack = fold(`${action.label} ${action.keywords}`);
    return words.every((word) => haystack.includes(word));
  });
}

const NEXT_THEME: Record<ThemeChoice, ThemeChoice> = { system: "light", light: "dark", dark: "system" };

/** Следующая тема по кругу: как в системе → светлая → тёмная. */
export function nextTheme(choice: ThemeChoice): ThemeChoice {
  return NEXT_THEME[choice];
}

/** macOS (и iPad/iPhone с клавиатурой): сочетание — ⌘K, иначе Ctrl K. */
export function isApplePlatform(): boolean {
  if (typeof navigator === "undefined") return false;
  const platform = (navigator as Navigator & { userAgentData?: { platform?: string } }).userAgentData?.platform ?? navigator.platform ?? "";
  return /mac|iphone|ipad/i.test(platform);
}

/** Подпись сочетания для кнопки. */
export function paletteShortcut(): string {
  return isApplePlatform() ? "⌘K" : "Ctrl K";
}

/** Ctrl+K / ⌘K — по физической клавише, поэтому и в русской раскладке. */
export function isPaletteHotkey(e: KeyboardEvent): boolean {
  if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey) return false;
  return e.code === "KeyK" || e.key.toLowerCase() === "k";
}

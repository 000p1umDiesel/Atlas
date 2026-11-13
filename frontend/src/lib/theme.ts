import { useSyncExternalStore } from "react";

export type ThemeChoice = "system" | "light" | "dark";

const KEY = "mb.theme";

/** Итоговая тема: ручной выбор побеждает системную настройку. */
export function resolveTheme(choice: ThemeChoice, systemDark: boolean): "light" | "dark" {
  if (choice === "system") return systemDark ? "dark" : "light";
  return choice;
}

/** Читает выбор из localStorage; недоступное хранилище или мусор дают "system". */
export function loadThemeChoice(): ThemeChoice {
  try {
    const value = localStorage.getItem(KEY);
    if (value === "light" || value === "dark" || value === "system") return value;
  } catch {
    // хранилище может быть недоступно (приватный режим, блокировка)
  }
  return "system";
}

export function saveThemeChoice(choice: ThemeChoice): void {
  try {
    localStorage.setItem(KEY, choice);
  } catch {
    // выбор просто не переживёт перезагрузку
  }
}

/** Ставит data-theme на <html>; "system" снимает атрибут, и работает prefers-color-scheme. */
export function applyTheme(choice: ThemeChoice): void {
  const root = document.documentElement;
  if (choice === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", choice);
}

// Один источник выбора темы на всё приложение: рейка, нижняя панель и /_design видят одно и
// то же значение (у каждого своего useState иконка устаревала).
let current: ThemeChoice | null = null;
const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Текущий выбор (при первом обращении — из localStorage). */
export function getThemeChoice(): ThemeChoice {
  current ??= loadThemeChoice();
  return current;
}

/** Меняет выбор: сохраняет, ставит data-theme и уведомляет всех подписчиков. */
export function setThemeChoice(next: ThemeChoice): void {
  saveThemeChoice(next);
  applyTheme(next);
  if (current === next) return;
  current = next;
  for (const listener of listeners) listener();
}

export function useTheme(): [ThemeChoice, (choice: ThemeChoice) => void] {
  return [useSyncExternalStore(subscribe, getThemeChoice), setThemeChoice];
}

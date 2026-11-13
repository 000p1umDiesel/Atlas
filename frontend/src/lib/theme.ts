import { useCallback, useState } from "react";

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

export function useTheme(): [ThemeChoice, (choice: ThemeChoice) => void] {
  const [choice, setChoice] = useState<ThemeChoice>(loadThemeChoice);
  const update = useCallback((next: ThemeChoice) => {
    saveThemeChoice(next);
    applyTheme(next);
    setChoice(next);
  }, []);
  return [choice, update];
}

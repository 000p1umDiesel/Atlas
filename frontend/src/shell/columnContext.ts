import { createContext, useContext } from "react";

export interface ShellColumnState {
  /** У раздела есть контекстная колонка. */
  available: boolean;
  /** Колонка свёрнута (десктоп). */
  collapsed: boolean;
  setCollapsed(collapsed: boolean): void;
  /** Открыть колонку: на десктопе развернуть, на узком экране — выезжающее меню. */
  open(): void;
}

export const ShellColumnContext = createContext<ShellColumnState>({
  available: false,
  collapsed: false,
  setCollapsed: () => {},
  open: () => {},
});

export function useShellColumn(): ShellColumnState {
  return useContext(ShellColumnContext);
}

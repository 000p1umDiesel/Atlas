import { createContext, useContext } from "react";

/**
 * Открыт оверлей оболочки (меню раздела или меню аккаунта на телефоне). Пока он открыт,
 * клавиши страницы молчат: Esc закрывает оверлей, а не снимает выделение под ним.
 */
export const ShellOverlayContext = createContext(false);

export function useShellOverlayOpen(): boolean {
  return useContext(ShellOverlayContext);
}

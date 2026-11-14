import { createContext, useCallback, useContext, type RefObject } from "react";

/**
 * Прокручиваемая область страницы — `<main>` оболочки, а не окно: `window.scrollTo` её не
 * двигает. Страницы прокручивают её через `useScrollMainToTop`, не ища элемент в DOM.
 */
export const MainScrollContext = createContext<RefObject<HTMLElement | null> | null>(null);

/** Прокрутка `<main>` к началу (вне оболочки — ничего не делает). */
export function useScrollMainToTop(): () => void {
  const ref = useContext(MainScrollContext);
  return useCallback(() => {
    if (ref?.current) ref.current.scrollTop = 0;
  }, [ref]);
}

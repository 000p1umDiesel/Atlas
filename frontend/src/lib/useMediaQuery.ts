import { useCallback, useSyncExternalStore } from "react";

/**
 * Совпадает ли медиазапрос, с подпиской на его изменения. Без `matchMedia` (тесты, SSR) —
 * `ssrDefault`.
 */
export function useMediaQuery(query: string, ssrDefault = true): boolean {
  const subscribe = useCallback(
    (cb: () => void) => {
      if (typeof matchMedia !== "function") return () => {};
      const mq = matchMedia(query);
      mq.addEventListener("change", cb);
      return () => mq.removeEventListener("change", cb);
    },
    [query],
  );
  const getSnapshot = () => (typeof matchMedia !== "function" ? ssrDefault : matchMedia(query).matches);
  return useSyncExternalStore(subscribe, getSnapshot, () => ssrDefault);
}

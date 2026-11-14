import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useMediaQuery } from "./useMediaQuery";

type Listener = () => void;

function stubMatchMedia(initial: boolean) {
  const listeners = new Set<Listener>();
  const state = { matches: initial };
  const removed = vi.fn();
  vi.stubGlobal("matchMedia", () => ({
    get matches() {
      return state.matches;
    },
    addEventListener: (_: string, cb: Listener) => listeners.add(cb),
    removeEventListener: (_: string, cb: Listener) => {
      removed();
      listeners.delete(cb);
    },
  }));
  return {
    listeners,
    removed,
    set(value: boolean) {
      state.matches = value;
      listeners.forEach((cb) => cb());
    },
  };
}

afterEach(() => vi.unstubAllGlobals());

describe("useMediaQuery", () => {
  it("без matchMedia возвращает значение по умолчанию", () => {
    vi.stubGlobal("matchMedia", undefined);
    expect(renderHook(() => useMediaQuery("(min-width: 1px)")).result.current).toBe(true);
    expect(renderHook(() => useMediaQuery("(min-width: 1px)", false)).result.current).toBe(false);
  });

  it("событие change обновляет значение", () => {
    const mq = stubMatchMedia(false);
    const { result } = renderHook(() => useMediaQuery("(min-width: 1px)"));
    expect(result.current).toBe(false);
    act(() => mq.set(true));
    expect(result.current).toBe(true);
  });

  it("отписывается при размонтировании", () => {
    const mq = stubMatchMedia(true);
    const { unmount } = renderHook(() => useMediaQuery("(min-width: 1px)"));
    expect(mq.listeners.size).toBe(1);
    unmount();
    expect(mq.removed).toHaveBeenCalledTimes(1);
    expect(mq.listeners.size).toBe(0);
  });
});

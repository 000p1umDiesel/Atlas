import { useNavigate, useSearch } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DEFAULT_FILTERS } from "./model";
import { parseGraphSearch, serializeGraphSearch, type GraphMode, type GraphSearch } from "./search";

/** Правка — объект или функция от свежего состояния URL (несколько правок подряд не теряются). */
export type GraphPatch = Partial<GraphSearch> | ((prev: GraphSearch) => Partial<GraphSearch>);
type Update = (patch: GraphPatch, opts?: { replace?: boolean }) => void;
type SetMode = (mode: GraphMode, patch?: Partial<GraphSearch>) => void;

/**
 * Переход в режим: раскрытые узлы и выбранный узел сбрасываются. `community` снимается при
 * любой смене режима, кроме входа в «Сообщество»: фильтр сообщества обзора прятал бы чужие
 * узлы окрестности или пути (а в их фильтрах его может и не быть в списке), выбор сообщества
 * в режиме «Сообщество» — тоже. При входе в «Сообщество» фильтр обзора становится выбором.
 * `patch` применяется поверх (центр, фокус, сообщество…).
 */
export function modePatch(prev: GraphSearch, mode: GraphMode, patch: Partial<GraphSearch> = {}): GraphSearch {
  const community = mode === "community" ? prev.community : null;
  return { ...prev, mode, expand: [], focus: null, community, ...patch };
}

/**
 * Фильтры графа живут в URL. Запись — только через `serializeGraphSearch`
 * (дефолты опускаются, списки через запятую), от свежего состояния URL.
 */
export function useGraphSearch(): { search: GraphSearch; update: Update; setMode: SetMode; reset(): void } {
  const raw = useSearch({ strict: false }) as Record<string, unknown>;
  const search = useMemo(() => parseGraphSearch(raw), [raw]);
  const navigate = useNavigate();

  const update = useCallback<Update>(
    (patch, opts) => {
      void navigate({
        to: "/graph",
        search: (prev) => {
          const s = parseGraphSearch(prev);
          return serializeGraphSearch({ ...s, ...(typeof patch === "function" ? patch(s) : patch) });
        },
        replace: opts?.replace,
      });
    },
    [navigate],
  );

  const setMode = useCallback<SetMode>(
    (mode, patch) => {
      void navigate({
        to: "/graph",
        search: (prev) => serializeGraphSearch(modePatch(parseGraphSearch(prev), mode, patch)),
      });
    },
    [navigate],
  );

  const reset = useCallback(() => {
    void navigate({
      to: "/graph",
      search: (prev) => {
        // Сброс фильтров не трогает режим, вид и выбранный узел (после сброса он снова виден);
        // в режиме «Сообщество» выбранное сообщество — не фильтр, оно остаётся.
        const s = parseGraphSearch(prev);
        return serializeGraphSearch({ ...s, ...DEFAULT_FILTERS, community: s.mode === "community" ? s.community : null });
      },
    });
  }, [navigate]);

  return { search, update, setMode, reset };
}

/**
 * Поле с отложенной записью (ползунок, строка поиска): показывает ввод сразу,
 * в URL пишет после паузы. Внешняя смена значения (сброс, «назад») заменяет черновик.
 */
export function useDebouncedField<T>(value: T, commit: (v: T) => void, delay = 180): [T, (v: T) => void] {
  const [draft, setDraft] = useState(value);
  const [seen, setSeen] = useState(value);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const lastCommitted = useRef<T>(value);
  const commitRef = useRef(commit);

  useEffect(() => {
    commitRef.current = commit;
  });

  if (!Object.is(value, seen)) {
    setSeen(value);
    // Эхо собственной записи не трогает черновик (пользователь мог уже ввести дальше).
    if (!Object.is(value, lastCommitted.current)) setDraft(value);
  }

  useEffect(() => {
    if (Object.is(value, lastCommitted.current)) return;
    lastCommitted.current = value;
    clearTimeout(timer.current);
  }, [value]);

  useEffect(() => () => clearTimeout(timer.current), []);

  const change = useCallback(
    (v: T) => {
      setDraft(v);
      clearTimeout(timer.current);
      timer.current = setTimeout(() => {
        lastCommitted.current = v;
        commitRef.current(v);
      }, delay);
    },
    [delay],
  );

  return [draft, change];
}

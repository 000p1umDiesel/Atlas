import { useNavigate, useSearch } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DEFAULT_FILTERS } from "./model";
import { parseGraphSearch, serializeGraphSearch, type GraphSearch } from "./search";

type Update = (patch: Partial<GraphSearch>, opts?: { replace?: boolean }) => void;

/**
 * Фильтры графа живут в URL. Запись — только через `serializeGraphSearch`
 * (дефолты опускаются, списки через запятую), от свежего состояния URL.
 */
export function useGraphSearch(): { search: GraphSearch; update: Update; reset(): void } {
  const raw = useSearch({ strict: false }) as Record<string, unknown>;
  const search = useMemo(() => parseGraphSearch(raw), [raw]);
  const navigate = useNavigate();

  const update = useCallback<Update>(
    (patch, opts) => {
      void navigate({
        to: "/graph",
        search: (prev) => serializeGraphSearch({ ...parseGraphSearch(prev), ...patch }),
        replace: opts?.replace,
      });
    },
    [navigate],
  );

  const reset = useCallback(() => {
    void navigate({
      to: "/graph",
      search: (prev) => {
        // Сброс фильтров не трогает вид и выбранный узел (после сброса он снова виден).
        const { view, focus } = parseGraphSearch(prev);
        return serializeGraphSearch({ ...DEFAULT_FILTERS, view, focus });
      },
    });
  }, [navigate]);

  return { search, update, reset };
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

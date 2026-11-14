import { useRouter, useRouterState } from "@tanstack/react-router";
import { useCallback, useMemo } from "react";
import { parsePlainSearch, stringifyPlainSearch } from "../lib/searchParams";

/** Что показывает правая панель: фрагмент документа или сущность. */
export type PanelRef = { kind: "chunk"; id: string } | { kind: "entity"; id: string };

/** Значение search-параметра `panel` (`chunk:<chunk_id>` | `entity:<entity_id>`); иначе `null`. */
export function parsePanel(raw: unknown): PanelRef | null {
  if (typeof raw !== "string") return null;
  const colon = raw.indexOf(":");
  const kind = raw.slice(0, colon);
  const id = raw.slice(colon + 1);
  if (colon < 0 || !id) return null;
  return kind === "chunk" || kind === "entity" ? { kind, id } : null;
}

export function formatPanel(ref: PanelRef): string {
  return `${ref.kind}:${ref.id}`;
}

/**
 * Правая панель через search-параметр `panel`: открыть, закрыть. Меняется только `panel`
 * (путь, прочие параметры и якорь остаются), с `replace` — без новой записи истории.
 */
export function usePanel(): { panel: PanelRef | null; open(ref: PanelRef): void; close(): void } {
  const router = useRouter();
  const raw = useRouterState({ select: (s) => (s.location.search as Record<string, unknown>).panel });
  const panel = useMemo(() => parsePanel(raw), [raw]);

  const setPanel = useCallback(
    (value: string | undefined) => {
      // Текущий адрес — на момент вызова, а не рендера.
      const { pathname, searchStr, hash } = router.latestLocation;
      const search: Record<string, unknown> = parsePlainSearch(searchStr);
      if (value === undefined) delete search.panel;
      else search.panel = value;
      const href = `${pathname}${stringifyPlainSearch(search)}${hash ? `#${hash}` : ""}`;
      void router.navigate({ href, replace: true });
    },
    [router],
  );

  return useMemo(
    () => ({
      panel,
      open: (ref: PanelRef) => setPanel(formatPanel(ref)),
      close: () => setPanel(undefined),
    }),
    [panel, setPanel],
  );
}

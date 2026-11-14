import { useRouter } from "@tanstack/react-router";
import { lazy, Suspense, useCallback, useId, useMemo, type ReactNode } from "react";
import { useNeighbors } from "../api/graph";
import { useTypeCatalog } from "../api/queries";
import { buildGraph, DEFAULT_FILTERS } from "../graph/model";
import { ErrorBoundary, ErrorNotice } from "../ui/ErrorNotice";
import { FormNote } from "../ui/form";
import { errorText } from "./parts";

// Sigma (WebGL) — отдельный чанк, общий со страницей графа: страница сущности его не тянет сразу.
const Graph2D = lazy(() => import("../graph/Graph2D").then((m) => ({ default: m.Graph2D })));

/** Глубина и предел узлов окрестности на странице сущности. */
const DEPTH = 1;
const LIMIT = 40;

function Box({ children }: { children: ReactNode }) {
  return <div className="grid h-72 place-items-center rounded-[14px] bg-card p-4 text-center shadow-card">{children}</div>;
}

/** Окрестность сущности на странице: соседи первого круга, клик по соседу — его страница. */
export function MiniGraph({ id }: { id: string }) {
  const query = useNeighbors(id, DEPTH, LIMIT);
  const catalog = useTypeCatalog();
  const router = useRouter();
  const headingId = useId();
  const graph = useMemo(() => (query.data ? buildGraph(query.data) : null), [query.data]);
  const onSelect = useCallback(
    (next: string) => {
      if (next !== id) void router.navigate({ href: `/entities/${encodeURIComponent(next)}` });
    },
    [id, router],
  );

  let body: ReactNode;
  if (query.isError) {
    body = <FormNote tone="error">{errorText(query.error)}</FormNote>;
  } else if (!graph) {
    body = (
      <Box>
        <p className="text-sm text-muted">Загружаю…</p>
      </Box>
    );
  } else if (query.data?.relations.length === 0) {
    // По ответу, а не по графу: связь с соседом, не попавшим в nodes (предел), — всё же связь.
    body = (
      <Box>
        <p className="text-sm text-muted">Связей пока нет.</p>
      </Box>
    );
  } else {
    body = (
      <div className="paper-grid h-72 overflow-hidden rounded-[14px] shadow-card">
        {/* Нет WebGL или чанк прежней сборки — сообщение только в этой области. */}
        <ErrorBoundary
          fallback={(error) => (
            <div className="grid h-full place-items-center p-2">
              <ErrorNotice error={error} />
            </div>
          )}
        >
          <Suspense fallback={<div className="grid h-full place-items-center text-sm text-muted">Загружаю…</div>}>
            <Graph2D
              graph={graph}
              filters={DEFAULT_FILTERS}
              entityTypes={catalog.order}
              typeLabel={catalog.typeLabel}
              selected={id}
              onSelect={onSelect}
              // Колесо мыши над мини-графом прокручивает страницу, а не масштабирует холст.
              wheelZoom={false}
            />
          </Suspense>
        </ErrorBoundary>
      </div>
    );
  }

  return (
    <section aria-labelledby={headingId} className="space-y-3">
      <h2 id={headingId} className="text-xl font-semibold tracking-tight">
        Окрестность
      </h2>
      {body}
    </section>
  );
}

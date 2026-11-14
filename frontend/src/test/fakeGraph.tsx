// Подмены 2D/3D-рендереров для тестов страниц: в jsdom нет WebGL.
import { useEffect } from "react";
import type { Graph2DApi, Graph2DProps } from "../graph/Graph2D";
import type { Graph3DProps } from "../graph/Graph3D";

export const fake2d = {
  api: {
    fit: vi.fn(),
    exportPng: vi.fn(async () => new Blob(["png"], { type: "image/png" })),
    dispose: vi.fn(),
    zoomIn: vi.fn(),
    zoomOut: vi.fn(),
    focusNode: vi.fn(),
    emphasize: vi.fn(),
  } satisfies Graph2DApi,
  last: null as Graph2DProps | null,
};

/** `ready: true` — 3D «с WebGL»: onReady отдаёт `api` (по умолчанию как без WebGL — не вызывается). */
export const fake3d = {
  api: {
    fit: vi.fn(),
    exportPng: vi.fn(async () => new Blob(["png"], { type: "image/png" })),
    dispose: vi.fn(),
    zoomIn: vi.fn(),
    zoomOut: vi.fn(),
    focusNode: vi.fn(),
    emphasize: vi.fn(),
  } satisfies Graph2DApi,
  ready: false,
  last: null as Graph3DProps | null,
};

export function FakeGraph2D(props: Graph2DProps) {
  fake2d.last = props;
  const { graph, onReady } = props;
  useEffect(() => {
    onReady?.(fake2d.api);
    // Осознанно: как настоящий Graph2D, onReady один раз на граф, а не на каждый новый колбэк.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph]);
  const first = graph.nodes()[0] ?? "";
  return (
    <div data-testid="graph2d">
      <button type="button" onClick={() => props.onSelect(first)}>
        выбрать узел
      </button>
      {props.onExpand && (
        <button type="button" onClick={() => props.onExpand?.(first)}>
          раскрыть узел
        </button>
      )}
    </div>
  );
}

/** 3D без WebGL: onReady не вызывается (как у настоящего Graph3D); с `fake3d.ready` — вызывается. */
export function FakeGraph3D(props: Graph3DProps) {
  fake3d.last = props;
  const { graph, onReady } = props;
  useEffect(() => {
    if (fake3d.ready) onReady?.(fake3d.api);
    // Осознанно: onReady один раз на граф, как у настоящего Graph3D.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph]);
  return <div data-testid="graph3d" />;
}

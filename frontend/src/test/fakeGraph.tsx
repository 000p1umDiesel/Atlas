// Подмены 2D/3D-рендереров для тестов страниц: в jsdom нет WebGL.
import { useEffect } from "react";
import type { Graph2DApi, Graph2DProps } from "../graph/Graph2D";
import type { Graph3DProps } from "../graph/Graph3D";

export const fake2d = {
  api: {
    fit: vi.fn(),
    exportPng: vi.fn(async () => new Blob(["png"], { type: "image/png" })),
    dispose: vi.fn(),
  } satisfies Graph2DApi,
  last: null as Graph2DProps | null,
};

export const fake3d = { last: null as Graph3DProps | null };

export function FakeGraph2D(props: Graph2DProps) {
  fake2d.last = props;
  const { graph, onReady } = props;
  useEffect(() => {
    onReady?.(fake2d.api);
    // как настоящий Graph2D: onReady один раз на граф
  }, [graph]);
  const first = graph.nodes()[0] ?? "";
  return (
    <div data-testid="graph2d">
      <button type="button" onClick={() => props.onSelect(first)}>
        выбрать узел
      </button>
    </div>
  );
}

/** 3D без WebGL: onReady не вызывается (как у настоящего Graph3D). */
export function FakeGraph3D(props: Graph3DProps) {
  fake3d.last = props;
  return <div data-testid="graph3d" />;
}

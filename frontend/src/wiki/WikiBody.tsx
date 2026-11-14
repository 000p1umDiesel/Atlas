import { useMemo } from "react";
import type { WikiPage } from "../api/types";
import { prepareWikiPage } from "../lib/markdown";
import { usePanel } from "../shell/panel";
import { Markdown } from "../ui/Markdown";

/** Тело страницы wiki: Markdown со сносками (открывают фрагмент в панели) и блок «Источники». */
export function WikiBody({ page }: { page: WikiPage }) {
  const { open } = usePanel();
  const prepared = useMemo(() => prepareWikiPage(page.content), [page]);
  const sources = useMemo(() => [...prepared.footnotes].sort(([, a], [, b]) => a - b), [prepared]);

  return (
    <>
      <Markdown
        text={prepared.body}
        env={{ footnotes: prepared.footnotes }}
        onRef={(ref) => ref.kind === "chunk" && open({ kind: "chunk", id: ref.id })}
      />
      {sources.length > 0 && (
        <section className="rounded-[14px] bg-card p-4 shadow-card sm:p-6">
          <h2 className="mb-3 text-xl font-semibold tracking-tight">Источники</h2>
          <ol aria-label="Источники" className="space-y-1.5">
            {sources.map(([id, n]) => {
              const name = page.sources[id];
              return (
                <li key={id}>
                  <button
                    type="button"
                    onClick={() => open({ kind: "chunk", id })}
                    className={`flex w-full gap-2 text-left text-sm underline-offset-4 [overflow-wrap:anywhere] hover:text-primary-ink hover:underline focus-visible:outline-2 focus-visible:outline-primary ${name ? "" : "text-muted"}`}
                  >
                    <span className="tabular-nums text-muted">{n}</span>
                    <span>{name ?? "фрагмент удалён"}</span>
                  </button>
                </li>
              );
            })}
          </ol>
        </section>
      )}
    </>
  );
}

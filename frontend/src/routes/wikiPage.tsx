import { useParams } from "@tanstack/react-router";
import { useMemo } from "react";
import { ApiError } from "../api/client";
import { useWikiPage } from "../api/wiki";
import { GHOST_BUTTON } from "../documents/JobsTable";
import { prepareWikiPage } from "../lib/markdown";
import { usePanel } from "../shell/panel";
import { FormNote } from "../ui/form";
import { Markdown } from "../ui/Markdown";
import { RouterAnchor } from "../ui/RouterAnchor";

const CRUMB =
  "text-sm text-muted underline-offset-4 hover:text-ink hover:underline focus-visible:outline-2 focus-visible:outline-primary";

function Article({ ref_ }: { ref_: { slug: string } | { pageId: string } }) {
  const query = useWikiPage(ref_);
  const { open } = usePanel();
  const page = query.data;
  const prepared = useMemo(() => (page ? prepareWikiPage(page.content) : null), [page]);
  const sources = useMemo(
    () => (prepared ? [...prepared.footnotes].sort(([, a], [, b]) => a - b) : []),
    [prepared],
  );

  return (
    <div className="mx-auto w-full max-w-3xl space-y-6 pt-2 pb-10 md:pt-6">
      <nav aria-label="Путь">
        <RouterAnchor href="/wiki" className={CRUMB}>
          Wiki
        </RouterAnchor>
      </nav>
      {query.isError ? (
        <FormNote tone="error">
          {query.error instanceof ApiError && query.error.status === 404
            ? "Страница не найдена: возможно, её удалили вместе с документом."
            : query.error instanceof ApiError
              ? query.error.detail
              : "Ошибка запроса"}
        </FormNote>
      ) : !page || !prepared ? (
        <p className="text-muted">Загружаю…</p>
      ) : (
        <article className="space-y-6">
          <header className="space-y-3">
            <h1 className="text-[2rem] leading-tight font-semibold tracking-tight [overflow-wrap:anywhere]">
              {page.title}
            </h1>
            <button type="button" className={GHOST_BUTTON} onClick={() => open({ kind: "entity", id: page.id })}>
              Сущность
            </button>
          </header>
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
        </article>
      )}
    </div>
  );
}

/** Страница wiki по slug или id; локальное состояние привязано к адресу, а не к разделу. */
export function WikiArticlePage() {
  const { slug, pageId } = useParams({ strict: false }) as { slug?: string; pageId?: string };
  const ref_ = pageId !== undefined ? { pageId } : { slug: slug ?? "" };
  return <Article key={pageId ?? slug} ref_={ref_} />;
}

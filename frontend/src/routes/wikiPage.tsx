import { useParams } from "@tanstack/react-router";
import { ApiError } from "../api/client";
import { useTypeCatalog } from "../api/queries";
import type { WikiBacklink } from "../api/types";
import { useWikiPage } from "../api/wiki";
import { usePanel } from "../shell/panel";
import { FormNote } from "../ui/form";
import { RouterAnchor } from "../ui/RouterAnchor";
import { CRUMB, GHOST_BUTTON } from "../ui/styles";
import { WikiBody } from "../wiki/WikiBody";

/** «Ссылаются на эту страницу»: страницы wiki с `[[ссылкой]]` сюда; пустой список — ничего. */
function Backlinks({ links }: { links: WikiBacklink[] }) {
  const catalog = useTypeCatalog();
  if (links.length === 0) return null;
  return (
    <section aria-labelledby="wiki-backlinks" className="border-t border-line pt-5">
      <h2 id="wiki-backlinks" className="text-sm font-semibold text-muted">
        Ссылаются на эту страницу <span className="font-normal tabular-nums">· {links.length}</span>
      </h2>
      <ul className="mt-3 grid gap-x-6 gap-y-1 sm:grid-cols-2">
        {links.map((link) => (
          <li key={link.id} className="min-w-0">
            <RouterAnchor
              href={`/wiki/${encodeURIComponent(link.slug)}`}
              className="group flex items-center gap-2 rounded-lg py-1 focus-visible:outline-2 focus-visible:outline-primary"
            >
              <span
                className="size-2 shrink-0 rounded-full"
                style={{ backgroundColor: catalog.typeColor(link.type) }}
                aria-hidden="true"
              />
              <span className="min-w-0 truncate font-medium underline-offset-4 group-hover:underline">{link.title}</span>
              <span className="shrink-0 text-xs text-muted">{catalog.typeLabel(link.type)}</span>
            </RouterAnchor>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Article({ ref_ }: { ref_: { slug: string } | { pageId: string } }) {
  const query = useWikiPage(ref_);
  const { open } = usePanel();
  const page = query.data;

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
      ) : !page ? (
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
          <WikiBody page={page} />
          <Backlinks links={page.backlinks ?? []} />
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

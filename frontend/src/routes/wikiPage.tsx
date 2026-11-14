import { useParams } from "@tanstack/react-router";
import { ApiError } from "../api/client";
import { useWikiPage } from "../api/wiki";
import { CRUMB, GHOST_BUTTON } from "../ui/styles";
import { usePanel } from "../shell/panel";
import { FormNote } from "../ui/form";
import { RouterAnchor } from "../ui/RouterAnchor";
import { WikiBody } from "../wiki/WikiBody";


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

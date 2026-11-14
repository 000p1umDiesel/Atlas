import { useNavigate, useSearch } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import { ApiError } from "../api/client";
import { useTypeCatalog } from "../api/queries";
import type { WikiListItem } from "../api/types";
import { useWikiList, WIKI_PAGE } from "../api/wiki";
import { GHOST_BUTTON } from "../documents/JobsTable";
import { countOf } from "../lib/format";
import type { TypeCatalog } from "../lib/typeCatalog";
import { FormNote } from "../ui/form";
import { RouterAnchor } from "../ui/RouterAnchor";

const PAGE_FORMS: [string, string, string] = ["страница", "страницы", "страниц"];
const DEBOUNCE_MS = 250;
const LINK =
  "text-sm underline-offset-4 [overflow-wrap:anywhere] hover:text-primary-ink hover:underline focus-visible:outline-2 focus-visible:outline-primary";

/** Группы по типу: порядок — как в настройках, неизвестные типы в конце по алфавиту. */
function groupByType(items: WikiListItem[], catalog: TypeCatalog) {
  const groups = new Map<string, WikiListItem[]>();
  for (const item of items) groups.set(item.entity_type, [...(groups.get(item.entity_type) ?? []), item]);
  const rank = (type: string) => {
    const at = catalog.order.indexOf(type);
    return at < 0 ? Number.MAX_SAFE_INTEGER : at;
  };
  return [...groups].sort(([a], [b]) => rank(a) - rank(b) || a.localeCompare(b));
}

/** Wiki: поиск по названию и псевдонимам, страницы по типам. Запрос живёт в `?q=`. */
export function WikiPage() {
  const q = (useSearch({ strict: false }) as { q?: unknown }).q;
  const urlQuery = typeof q === "string" ? q.trim() : "";
  const navigate = useNavigate();
  const catalog = useTypeCatalog();
  const [text, setText] = useState(typeof q === "string" ? q : "");
  const [count, setCount] = useState(WIKI_PAGE);
  const list = useWikiList(urlQuery, count);

  // Задержка 250 мс: в адрес (и в запрос) попадает обрезанное значение; `panel` сохраняется.
  useEffect(() => {
    const next = text.trim();
    if (next === urlQuery) return;
    const timer = setTimeout(() => {
      setCount(WIKI_PAGE);
      void navigate({
        to: ".",
        search: ((prev: Record<string, unknown>) => ({ ...prev, q: next || undefined })) as never,
        replace: true,
      });
    }, DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [text, urlQuery, navigate]);

  const groups = useMemo(() => groupByType(list.data?.items ?? [], catalog), [list.data, catalog]);
  const items = list.data?.items ?? [];

  return (
    <div className="mx-auto w-full max-w-4xl space-y-6 pt-2 pb-10 md:pt-6">
      <header className="space-y-2">
        <h1 className="text-[2.25rem] leading-tight font-semibold tracking-tight">Wiki</h1>
        <p className="text-muted">
          Страницы о людях, понятиях и работах из ваших документов. Их пишет модель по фрагментам, на которые
          ссылается каждая фраза.
        </p>
      </header>
      <input
        type="search"
        value={text}
        onChange={(event) => setText(event.target.value)}
        placeholder="Найти страницу"
        aria-label="Поиск по wiki"
        maxLength={200}
        className="w-full rounded-[10px] bg-card px-3.5 py-2.5 shadow-card focus-visible:outline-2 focus-visible:outline-primary"
      />
      {list.isError && (
        <FormNote tone="error">{list.error instanceof ApiError ? list.error.detail : "Ошибка запроса"}</FormNote>
      )}
      {list.data === undefined ? (
        list.isPending && <p className="text-muted">Загружаю…</p>
      ) : items.length === 0 ? (
        <p className="text-muted">
          {urlQuery
            ? "Ничего не нашлось. Попробуйте другое слово или псевдоним."
            : "Wiki пока пуста: страницы появятся после обработки документов."}
        </p>
      ) : (
        <>
          <p className="text-sm text-muted">
            {list.data.more && "Показаны первые "}
            {countOf(items.length, PAGE_FORMS)}
          </p>
          <div className="space-y-5">
            {groups.map(([type, pages]) => (
              <section key={type} className="rounded-[14px] bg-card p-4 shadow-card sm:p-6">
                <h2 className="mb-2 flex items-center gap-2 text-lg font-semibold tracking-tight">
                  <span
                    className="size-2.5 shrink-0 rounded-full"
                    style={{ backgroundColor: catalog.typeColor(type) }}
                    aria-hidden="true"
                  />
                  {catalog.typeLabel(type)}
                </h2>
                <ul className="grid gap-x-6 gap-y-1.5 sm:grid-cols-2">
                  {pages.map((page) => (
                    <li key={page.page_id} className="min-w-0">
                      <RouterAnchor href={`/wiki/${encodeURIComponent(page.slug)}`} className={LINK}>
                        {page.title}
                      </RouterAnchor>
                    </li>
                  ))}
                </ul>
              </section>
            ))}
          </div>
          {list.data.more && (
            <button
              type="button"
              disabled={list.isPlaceholderData}
              onClick={() => setCount((n) => n + WIKI_PAGE)}
              className={GHOST_BUTTON}
            >
              Показать ещё
            </button>
          )}
        </>
      )}
    </div>
  );
}

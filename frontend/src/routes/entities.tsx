import { X } from "lucide-react";
import { useEffect, useId, useMemo, useRef } from "react";
import { ApiError } from "../api/client";
import { communityTitle, useCommunities, useEntityList } from "../api/entities";
import { useTypeCatalog } from "../api/queries";
import { GHOST_BUTTON } from "../documents/JobsTable";
import { EntityTable } from "../entities/EntityTable";
import {
  effectiveOrder,
  effectiveSort,
  hasFilters,
  useEntitiesSearch,
  withOrder,
  withSort,
  type EntityOrder,
  type EntitySort,
} from "../entities/search";
import { useDebouncedField } from "../graph/useGraphSearch";
import { countOf, formatInt } from "../lib/format";
import { FormNote } from "../ui/form";
import { RouterAnchor } from "../ui/RouterAnchor";

const DEBOUNCE_MS = 250;
const ENTITY_FORMS: [string, string, string] = ["сущность", "сущности", "сущностей"];
const SORT_LABELS: Record<EntitySort, string> = {
  relevance: "По релевантности",
  mentions: "По упоминаниям",
  pagerank: "По PageRank",
  degree: "По связям",
  name: "По имени",
};
const SELECT =
  "rounded-[10px] border border-line bg-card px-3 py-2 text-sm shadow-card outline-none focus:border-primary disabled:opacity-60";
const CHIP =
  "inline-flex items-center gap-1.5 rounded-full border border-line bg-card py-1 pr-1.5 pl-2.5 text-[13px] transition-colors hover:border-primary/60 focus-visible:outline-2 focus-visible:outline-primary";
/** Сообществ, по которым ищется подпись фильтра (тот же запрос, что в колонке). */
const COMMUNITIES_SHOWN = 12;

/** Подгрузка следующей страницы, когда страж внизу списка виден (если браузер умеет IO). */
function useLoadMore(enabled: boolean, load: () => void) {
  const ref = useRef<HTMLDivElement>(null);
  const loadRef = useRef(load);
  useEffect(() => {
    loadRef.current = load;
  });
  useEffect(() => {
    const el = ref.current;
    if (!enabled || !el || typeof IntersectionObserver !== "function") return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) loadRef.current();
      },
      { rootMargin: "400px 0px" },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [enabled]);
  return ref;
}

/** Каталог сущностей: поиск, фильтры (колонка), сортировка, подгрузка страницами по 50. */
export function EntitiesPage() {
  const { search, update } = useEntitiesSearch();
  const catalog = useTypeCatalog();
  const list = useEntityList(search);
  const communities = useCommunities(COMMUNITIES_SHOWN);
  const ids = { sort: useId(), order: useId() };

  const [text, setText] = useDebouncedField(
    search.q,
    (q) => update((s) => ({ ...s, q, sort: s.sort === "relevance" && !q.trim() ? null : s.sort }), { replace: true }),
    DEBOUNCE_MS,
  );

  const items = useMemo(() => list.data?.pages.flatMap((p) => p.items) ?? [], [list.data]);
  const total = list.data?.pages[0]?.total ?? 0;
  const sort = effectiveSort(search);
  const order = effectiveOrder(search);
  const filtered = hasFilters(search);
  const showPagerank = items.some((item) => item.pagerank !== null);
  const canLoadMore = list.hasNextPage && !list.isFetchingNextPage && !list.isPlaceholderData;
  const loadMore = () => {
    if (canLoadMore) void list.fetchNextPage();
  };
  const sentinel = useLoadMore(canLoadMore, loadMore);

  const sortOptions = (Object.keys(SORT_LABELS) as EntitySort[]).filter((s) => s !== "relevance" || search.q.trim());
  const onSort = (column: EntitySort) =>
    update((s) =>
      effectiveSort(s) === column ? withOrder(s, effectiveOrder(s) === "asc" ? "desc" : "asc") : withSort(s, column),
    );
  const resetFilters = () => update((s) => ({ ...s, q: "", types: [], community: null, minMentions: 0 }));

  const communityName = (id: string) => {
    const hit = communities.data?.items.find((c) => c.community === id);
    return hit ? communityTitle(hit) : `Сообщество ${id}`;
  };

  return (
    <div className="mx-auto w-full max-w-5xl space-y-5 pt-2 pb-10 md:pt-6">
      <header className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h1 className="text-[2.25rem] leading-tight font-semibold tracking-tight">Сущности</h1>
        {list.data && (
          <p className="text-sm text-muted tabular-nums" aria-live="polite">
            {countOf(total, ENTITY_FORMS)}
          </p>
        )}
      </header>

      <div className="flex flex-wrap items-center gap-2">
        <input
          type="search"
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder="Имя или псевдоним"
          aria-label="Поиск сущностей"
          maxLength={200}
          autoComplete="off"
          className="min-w-0 flex-[1_1_16rem] rounded-[10px] bg-card px-3.5 py-2.5 shadow-card focus-visible:outline-2 focus-visible:outline-primary"
        />
        <label htmlFor={ids.sort} className="sr-only">
          Сортировка
        </label>
        <select
          id={ids.sort}
          value={sort}
          onChange={(event) => update((s) => withSort(s, event.target.value as EntitySort))}
          className={SELECT}
        >
          {sortOptions.map((s) => (
            <option key={s} value={s}>
              {SORT_LABELS[s]}
            </option>
          ))}
        </select>
        <label htmlFor={ids.order} className="sr-only">
          Порядок
        </label>
        <select
          id={ids.order}
          value={order}
          disabled={sort === "relevance"}
          onChange={(event) => update((s) => withOrder(s, event.target.value as EntityOrder))}
          className={SELECT}
        >
          <option value="desc">{sort === "name" ? "Я → А" : "По убыванию"}</option>
          <option value="asc">{sort === "name" ? "А → Я" : "По возрастанию"}</option>
        </select>
      </div>

      {(search.types.length > 0 || search.community !== null || search.minMentions > 0) && (
        <ul aria-label="Активные фильтры" className="flex flex-wrap gap-1.5">
          {search.types.map((type) => (
            <li key={type}>
              <button
                type="button"
                aria-label={`Убрать фильтр «${catalog.typeLabel(type)}»`}
                onClick={() => update((s) => ({ ...s, types: s.types.filter((t) => t !== type) }))}
                className={CHIP}
              >
                <span
                  className="size-2.5 rounded-full"
                  style={{ backgroundColor: catalog.typeColor(type) }}
                  aria-hidden="true"
                />
                {catalog.typeLabel(type)}
                <X size={13} aria-hidden="true" className="text-muted" />
              </button>
            </li>
          ))}
          {search.community !== null && (
            <li>
              <button
                type="button"
                aria-label="Убрать фильтр по сообществу"
                onClick={() => update((s) => ({ ...s, community: null }))}
                className={CHIP}
              >
                <span className="max-w-64 truncate">{communityName(search.community)}</span>
                <X size={13} aria-hidden="true" className="text-muted" />
              </button>
            </li>
          )}
          {search.minMentions > 0 && (
            <li>
              <button
                type="button"
                aria-label="Убрать порог упоминаний"
                onClick={() => update((s) => ({ ...s, minMentions: 0 }))}
                className={CHIP}
              >
                от {formatInt(search.minMentions)} упоминаний
                <X size={13} aria-hidden="true" className="text-muted" />
              </button>
            </li>
          )}
        </ul>
      )}

      {list.isError && (
        <FormNote tone="error">{list.error instanceof ApiError ? list.error.detail : "Ошибка запроса"}</FormNote>
      )}

      {list.data === undefined ? (
        list.isPending && <p className="text-muted">Загружаю…</p>
      ) : items.length === 0 ? (
        filtered ? (
          <div className="space-y-3 rounded-[14px] bg-card p-6 shadow-card">
            <p className="text-muted">Ничего не нашлось. Ослабьте фильтры.</p>
            <button type="button" onClick={resetFilters} className={GHOST_BUTTON}>
              Сбросить фильтры
            </button>
          </div>
        ) : (
          <p className="rounded-[14px] bg-card p-6 text-muted shadow-card">
            Сущностей пока нет:{" "}
            <RouterAnchor
              href="/documents"
              className="text-primary-ink underline underline-offset-4 focus-visible:outline-2 focus-visible:outline-primary"
            >
              загрузите документы
            </RouterAnchor>
            .
          </p>
        )
      ) : (
        <section
          className={`rounded-[14px] bg-card p-4 shadow-card transition-opacity sm:p-6 ${list.isPlaceholderData ? "opacity-60" : ""}`}
          aria-busy={list.isPlaceholderData || list.isFetchingNextPage}
        >
          <EntityTable
            items={items}
            catalog={catalog}
            showPagerank={showPagerank}
            sort={sort}
            order={order}
            onSort={onSort}
          />
          {list.hasNextPage && (
            <div ref={sentinel} className="mt-4 flex items-center gap-3">
              <button type="button" disabled={!canLoadMore} onClick={loadMore} className={GHOST_BUTTON}>
                Показать ещё
              </button>
              <span className="text-sm text-muted tabular-nums">
                {formatInt(items.length)} из {formatInt(total)}
              </span>
            </div>
          )}
        </section>
      )}
    </div>
  );
}

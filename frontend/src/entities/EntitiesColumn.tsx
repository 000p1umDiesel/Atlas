import type { ReactNode } from "react";
import { ApiError } from "../api/client";
import { communityTitle, useCommunities } from "../api/entities";
import { useTypeCatalog } from "../api/queries";
import { formatInt } from "../lib/format";
import { RouterAnchor } from "../ui/RouterAnchor";
import { useEntitiesSearch } from "./search";

/** Сообществ в колонке: самые крупные. */
const COMMUNITIES_SHOWN = 12;

const ITEM =
  "flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-sm transition-colors hover:bg-ink/[0.06] focus-visible:outline-2 focus-visible:outline-primary aria-pressed:bg-primary/15 aria-pressed:font-medium aria-pressed:text-primary-ink";

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-1.5">
      <h3 className="px-2.5 font-sans text-[13px] font-medium text-muted">{title}</h3>
      {children}
    </section>
  );
}

/** Колонка каталога: фильтр по типам (только встречающимся) и по сообществам анализа. */
export function EntitiesColumn() {
  const { search, update } = useEntitiesSearch();
  const catalog = useTypeCatalog();
  const communities = useCommunities(COMMUNITIES_SHOWN);
  const counts = new Map(catalog.types.map((t) => [t.type, t.count]));

  const toggleType = (type: string) =>
    update((s) => ({
      ...s,
      types: s.types.includes(type) ? s.types.filter((t) => t !== type) : [...s.types, type],
    }));
  const toggleCommunity = (community: string) =>
    update((s) => ({ ...s, community: s.community === community ? null : community }));

  return (
    <nav aria-label="Фильтры сущностей" className="space-y-6 pb-4">
      <Section title="Типы">
        {catalog.present.length === 0 ? (
          <p className="px-2.5 text-sm text-muted">Типов пока нет.</p>
        ) : (
          <ul className="space-y-0.5">
            {catalog.present.map((type) => (
              <li key={type}>
                <button
                  type="button"
                  aria-pressed={search.types.includes(type)}
                  onClick={() => toggleType(type)}
                  title={catalog.typeInfo(type).description || undefined}
                  className={ITEM}
                >
                  <span
                    className="size-2.5 shrink-0 rounded-full"
                    style={{ backgroundColor: catalog.typeColor(type) }}
                    aria-hidden="true"
                  />
                  <span className="min-w-0 flex-1 truncate">{catalog.typeLabel(type)}</span>
                  <span className="text-muted tabular-nums">{formatInt(counts.get(type) ?? 0)}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="Сообщества">
        {communities.isPending ? (
          <p className="px-2.5 text-sm text-muted">Загружаю…</p>
        ) : communities.isError ? (
          <p className="px-2.5 text-sm text-danger">
            {communities.error instanceof ApiError ? communities.error.detail : "Ошибка запроса"}
          </p>
        ) : !communities.data.ready ? (
          <p className="px-2.5 text-sm text-muted">Сообщества появятся после анализа графа.</p>
        ) : communities.data.items.length === 0 ? (
          <p className="px-2.5 text-sm text-muted">Сообществ нет.</p>
        ) : (
          <ul className="space-y-0.5">
            {communities.data.items.map((c) => (
              <li key={c.community}>
                <button
                  type="button"
                  aria-pressed={search.community === c.community}
                  onClick={() => toggleCommunity(c.community)}
                  className={ITEM}
                >
                  <span className="min-w-0 flex-1 [overflow-wrap:anywhere]">{communityTitle(c)}</span>
                  <span className="text-muted tabular-nums">{formatInt(c.size)}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
        <RouterAnchor
          href="/graph?mode=community"
          className="block px-2.5 pt-1 text-[13px] text-primary-ink underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-primary"
        >
          Все сообщества на графе
        </RouterAnchor>
      </Section>
    </nav>
  );
}

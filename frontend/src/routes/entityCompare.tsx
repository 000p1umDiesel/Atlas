import { useNavigate, useSearch } from "@tanstack/react-router";
import { ArrowLeftRight, MessageCircleQuestion, Route } from "lucide-react";
import type { ReactNode } from "react";
import { ApiError } from "../api/client";
import { COMMUNITIES_LIMIT, communityTitle, useCommunities, useEntityComparison } from "../api/entities";
import { usePath } from "../api/graph";
import { useTypeCatalog } from "../api/queries";
import type { ComparePredicate, EntityCard, EntityComparison, PathView } from "../api/types";
import { errorText } from "../entities/parts";
import { predicateLabel } from "../graph/insights";
import { graphLink } from "../graph/search";
import { countOf, formatInt, formatScore } from "../lib/format";
import { stringifyPlainSearch } from "../lib/searchParams";
import { EntityPicker } from "../ui/EntityPicker";
import { FormNote } from "../ui/form";
import { RouterAnchor } from "../ui/RouterAnchor";
import { CRUMB, GHOST_BUTTON, NAME_LINK, TABLE, TD, TH } from "../ui/styles";

/** Длина кратчайшего пути, когда прямой связи нет. */
const PATH_HOPS = 4;
/** Путей показываем не больше. */
const PATHS_SHOWN = 3;
const ACTION = `${GHOST_BUTTON} gap-1.5`;

/** search-параметры `/entities/compare`: `a`, `b` (непустые строки) и `panel`. */
export function validateCompareSearch(raw: Record<string, unknown>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const key of ["a", "b", "panel"] as const) {
    const value = raw[key];
    if (typeof value === "string" && value.trim() !== "") out[key] = value;
  }
  return out;
}

/** Вопрос в чат: как связаны две сущности (заготовка, её можно поправить). */
export function askHowRelated(a: string, b: string): string {
  return `Как связаны «${a}» и «${b}»?`;
}

function Section({ title, count, children }: { title: string; count?: number; children: ReactNode }) {
  return (
    <section className="space-y-3" aria-label={title}>
      <h2 className="text-lg font-semibold tracking-tight">
        {title}
        {count !== undefined && <span className="ml-2 text-base font-normal text-muted tabular-nums">{formatInt(count)}</span>}
      </h2>
      <div className="min-w-0 rounded-[14px] bg-card p-4 shadow-card sm:p-5">{children}</div>
    </section>
  );
}

function Metric({ label, children, accent = false }: { label: string; children: ReactNode; accent?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-line py-1.5 last:border-b-0">
      <dt className="text-sm text-muted">{label}</dt>
      <dd className={`min-w-0 truncate text-right text-sm font-medium tabular-nums ${accent ? "text-primary-ink" : ""}`}>{children}</dd>
    </div>
  );
}

/** Карточка одной стороны: тип, метрики, сообщество (общее подсвечено). */
function Side({ entity, neighbors, sharedCommunity }: { entity: EntityCard; neighbors: number; sharedCommunity: boolean }) {
  const catalog = useTypeCatalog();
  const communities = useCommunities(COMMUNITIES_LIMIT, entity.community !== null);
  const community = entity.community
    ? (communities.data?.items.find((c) => c.community === entity.community) ?? null)
    : null;
  return (
    <article className="min-w-0 rounded-[14px] bg-card p-4 shadow-card sm:p-5" aria-label={entity.name}>
      <RouterAnchor href={`/entities/${encodeURIComponent(entity.entity_id)}`} className={`${NAME_LINK} text-lg font-semibold`}>
        {entity.name}
      </RouterAnchor>
      <p className="mt-1 flex items-center gap-1.5 text-sm text-muted">
        <span className="size-2.5 rounded-full" style={{ backgroundColor: catalog.typeColor(entity.type) }} aria-hidden="true" />
        {catalog.typeLabel(entity.type)}
      </p>
      <dl className="mt-3">
        <Metric label="Упоминания">{formatInt(entity.mention_count)}</Metric>
        <Metric label="Документы">{formatInt(entity.documents_count)}</Metric>
        <Metric label="Связи">{formatInt(entity.degree)}</Metric>
        <Metric label="Соседи">{formatInt(neighbors)}</Metric>
        <Metric label="PageRank">{entity.pagerank !== null ? formatScore(entity.pagerank) : "—"}</Metric>
        <Metric label="Сообщество" accent={sharedCommunity}>
          {entity.community === null ? "—" : community ? communityTitle(community) : entity.community}
        </Metric>
      </dl>
      {sharedCommunity && <p className="mt-2 text-xs text-primary-ink">Обе в одном сообществе</p>}
      {entity.has_page && entity.slug && (
        <RouterAnchor href={`/wiki/${encodeURIComponent(entity.slug)}`} className={`${CRUMB} mt-3 inline-block`}>
          Страница wiki
        </RouterAnchor>
      )}
    </article>
  );
}

/** «→ wrote» (от сравниваемой к соседу) или «← designed» (от соседа к ней). */
function Predicates({ items }: { items: ComparePredicate[] }) {
  return (
    <ul className="space-y-0.5">
      {items.map((p) => (
        <li key={`${p.predicate}:${p.outgoing}`} className="whitespace-nowrap">
          <span className="text-muted" aria-label={p.outgoing ? "к соседу" : "от соседа"}>
            {p.outgoing ? "→" : "←"}
          </span>{" "}
          {predicateLabel(p.predicate)}
        </li>
      ))}
    </ul>
  );
}

/** Цепочка пути: имена узлов через предикаты. */
function PathChain({ path }: { path: PathView }) {
  const names = new Map(path.nodes.map((n) => [n.entity_id, n.name]));
  const order = path.nodes.map((n) => n.entity_id);
  return (
    <p className="leading-relaxed [overflow-wrap:anywhere]">
      {order.map((id, i) => {
        const rel = i > 0 ? path.relations[i - 1] : undefined;
        return (
          <span key={`${id}-${i}`}>
            {rel && <span className="mx-1.5 text-sm text-muted">— {predicateLabel(rel.predicate)} —</span>}
            <RouterAnchor href={`/entities/${encodeURIComponent(id)}`} className={NAME_LINK}>
              {names.get(id) ?? id}
            </RouterAnchor>
          </span>
        );
      })}
    </p>
  );
}

function ShortestPath({ a, b }: { a: string; b: string }) {
  const query = usePath(a, b, PATH_HOPS);
  if (query.isError) return <FormNote tone="error">{errorText(query.error)}</FormNote>;
  if (!query.data) return <p className="text-sm text-muted">Ищу путь…</p>;
  const { paths, timed_out } = query.data;
  if (paths.length === 0)
    return (
      <p className="text-sm text-muted">
        {timed_out ? "Поиск пути занял слишком долго." : `Путей длиной до ${PATH_HOPS} шагов нет: в графе сущности не связаны.`}
      </p>
    );
  return (
    <ol className="space-y-2">
      {paths.slice(0, PATHS_SHOWN).map((path, i) => (
        <li key={i}>
          <PathChain path={path} />
        </li>
      ))}
    </ol>
  );
}

function Result({ data }: { data: EntityComparison }) {
  const { a, b } = data;
  const sharedCommunity = a.community !== null && a.community === b.community;
  const neighbors = data.shared_neighbors;
  const docs = data.shared_documents;
  const facts = [
    data.direct.length > 0 ? countOf(data.direct.length, ["прямая связь", "прямые связи", "прямых связей"]) : "нет прямой связи",
    countOf(neighbors.total, ["общий сосед", "общих соседа", "общих соседей"]),
    countOf(docs.length, ["общий документ", "общих документа", "общих документов"]),
    `вместе в ${countOf(data.co_mentions, ["фрагменте", "фрагментах", "фрагментах"])}`,
  ];
  return (
    <div className="space-y-8">
      <div className="grid gap-4 sm:grid-cols-2">
        <Side entity={a} neighbors={data.neighbors.a} sharedCommunity={sharedCommunity} />
        <Side entity={b} neighbors={data.neighbors.b} sharedCommunity={sharedCommunity} />
      </div>

      <div className="space-y-3">
        <p className="font-serif text-lg leading-relaxed">{facts.join(" · ")}</p>
        <div role="group" aria-label="Действия" className="flex flex-wrap gap-2">
          <RouterAnchor href={`/chat${stringifyPlainSearch({ ask: askHowRelated(a.name, b.name), mode: "graph" })}`} className={ACTION}>
            <MessageCircleQuestion size={15} aria-hidden="true" />
            Спросить, как связаны
          </RouterAnchor>
          <RouterAnchor href={graphLink({ mode: "path", source: a.entity_id, target: b.entity_id })} className={ACTION}>
            <Route size={15} aria-hidden="true" />
            Путь на графе
          </RouterAnchor>
        </div>
      </div>

      {data.direct.length > 0 ? (
        <Section title="Прямые связи" count={data.direct.length}>
          <ul className="space-y-3">
            {data.direct.map((r, i) => (
              <li key={`${r.src_id}-${r.predicate}-${i}`}>
                <p className="font-medium">
                  {r.src_name} <span className="text-muted">— {predicateLabel(r.predicate)} →</span> {r.dst_name}
                </p>
                {r.description && <p className="mt-0.5 text-sm text-muted">{r.description}</p>}
              </li>
            ))}
          </ul>
        </Section>
      ) : (
        <Section title="Кратчайший путь">
          <ShortestPath a={a.entity_id} b={b.entity_id} />
        </Section>
      )}

      <Section title="Общие соседи" count={neighbors.total}>
        {neighbors.items.length === 0 ? (
          <p className="text-sm text-muted">Нет сущностей, связанных с обеими.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className={TABLE}>
              <thead>
                <tr>
                  <th className={TH}>Сосед</th>
                  <th className={TH}>Связь с «{a.name}»</th>
                  <th className={TH}>Связь с «{b.name}»</th>
                </tr>
              </thead>
              <tbody>
                {neighbors.items.map((n) => (
                  <tr key={n.entity_id}>
                    <td className={TD}>
                      <RouterAnchor href={`/entities/${encodeURIComponent(n.entity_id)}`} className={NAME_LINK}>
                        {n.name}
                      </RouterAnchor>
                    </td>
                    <td className={TD}>
                      <Predicates items={n.a_predicates} />
                    </td>
                    <td className={TD}>
                      <Predicates items={n.b_predicates} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {neighbors.total > neighbors.items.length && (
              <p className="mt-2 text-xs text-muted">
                Показаны {formatInt(neighbors.items.length)} из {formatInt(neighbors.total)} с самыми сильными связями.
              </p>
            )}
          </div>
        )}
      </Section>

      <Section title="Общие документы" count={docs.length}>
        {docs.length === 0 ? (
          <p className="text-sm text-muted">Нет документов, где упомянуты обе.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className={TABLE}>
              <thead>
                <tr>
                  <th className={TH}>Документ</th>
                  <th className={`${TH} text-right`}>{a.name}</th>
                  <th className={`${TH} text-right`}>{b.name}</th>
                </tr>
              </thead>
              <tbody>
                {docs.map((d) => (
                  <tr key={d.doc_id}>
                    <td className={TD}>
                      <RouterAnchor href={`/documents/${encodeURIComponent(d.doc_id)}`} className={NAME_LINK}>
                        {d.name}
                      </RouterAnchor>
                    </td>
                    <td className={`${TD} text-right tabular-nums`}>{formatInt(d.a_mentions)}</td>
                    <td className={`${TD} text-right tabular-nums`}>{formatInt(d.b_mentions)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="mt-2 text-xs text-muted">Числа — фрагменты документа, где упомянута сущность.</p>
          </div>
        )}
      </Section>
    </div>
  );
}

/** Сравнение двух сущностей `/entities/compare?a=&b=`: выбор в URL, результат — когда выбраны обе. */
export function EntityComparePage() {
  const search = useSearch({ strict: false }) as Record<string, string | undefined>;
  const navigate = useNavigate();
  const a = search.a ?? null;
  const b = search.b ?? null;
  const query = useEntityComparison(a, b);

  const set = (patch: { a?: string | null; b?: string | null }) =>
    void navigate({
      to: "/entities/compare",
      search: (prev: Record<string, unknown>) => validateCompareSearch({ ...prev, ...patch }),
      replace: true,
    });

  let body: ReactNode;
  if (a === null || b === null) {
    body = <p className="text-muted">Выберите две сущности: покажу, что у них общего в графе и в документах.</p>;
  } else if (a === b) {
    body = <FormNote tone="error">Выберите две разные сущности.</FormNote>;
  } else if (query.isError) {
    const notFound = query.error instanceof ApiError && query.error.status === 404;
    body = <FormNote tone="error">{notFound ? "Одна из сущностей не найдена: возможно, её удалили вместе с документом." : errorText(query.error)}</FormNote>;
  } else if (!query.data) {
    body = <p className="text-muted">Сравниваю…</p>;
  } else {
    body = <Result data={query.data} />;
  }

  return (
    <div className="mx-auto w-full max-w-5xl space-y-6 pt-2 pb-10 md:pt-6">
      <nav aria-label="Путь">
        <RouterAnchor href="/entities" className={CRUMB}>
          Сущности
        </RouterAnchor>
      </nav>
      <h1 className="text-[2rem] leading-tight font-semibold tracking-tight">Сравнение сущностей</h1>
      <div className="grid items-end gap-3 sm:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)]">
        <EntityPicker label="Первая" value={a} exclude={b ?? undefined} onChange={(id) => set({ a: id })} />
        <button
          type="button"
          className={`${GHOST_BUTTON} justify-center`}
          disabled={a === null && b === null}
          onClick={() => set({ a: b, b: a })}
          aria-label="Поменять местами"
          title="Поменять местами"
        >
          <ArrowLeftRight size={16} aria-hidden="true" />
        </button>
        <EntityPicker label="Вторая" value={b} exclude={a ?? undefined} onChange={(id) => set({ b: id })} />
      </div>
      {body}
    </div>
  );
}

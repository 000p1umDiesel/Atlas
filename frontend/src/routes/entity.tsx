import { useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { ArrowLeftRight, BookOpen, MessageCircleQuestion, Network, Route } from "lucide-react";
import { useId, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { ApiError } from "../api/client";
import { COMMUNITIES_LIMIT, communityTitle, useCommunities, useEntity } from "../api/entities";
import { useTypeCatalog } from "../api/queries";
import type { EntityCard } from "../api/types";
import { useWikiPage } from "../api/wiki";
import { CRUMB, GHOST_BUTTON } from "../ui/styles";
import { DocumentsTab } from "../entities/DocumentsTab";
import { MentionsTab } from "../entities/MentionsTab";
import { MiniGraph } from "../entities/MiniGraph";
import { errorText } from "../entities/parts";
import { RelationsTab, type RelationFilters } from "../entities/RelationsTab";
import { graphLink } from "../graph/search";
import { formatInt, formatScore } from "../lib/format";
import { stringifyPlainSearch } from "../lib/searchParams";
import { FormNote } from "../ui/form";
import { RouterAnchor } from "../ui/RouterAnchor";
import { WikiBody } from "../wiki/WikiBody";

export type EntityTab = "relations" | "mentions" | "documents" | "wiki";

const TABS: readonly EntityTab[] = ["relations", "mentions", "documents", "wiki"];
const NOT_FOUND = "Сущность не найдена: возможно, её удалили вместе с документом.";

/** Вкладка из URL; неизвестная — «Связи». */
export function parseEntityTab(raw: unknown): EntityTab {
  return TABS.includes(raw as EntityTab) ? (raw as EntityTab) : "relations";
}

/** search-параметры `/entities/$id`: только `tab` (не по умолчанию) и `panel`. */
export function validateEntitySearch(raw: Record<string, unknown>): Record<string, string> {
  const tab = parseEntityTab(raw.tab);
  const out: Record<string, string> = tab === "relations" ? {} : { tab };
  return typeof raw.panel === "string" ? { ...out, panel: raw.panel } : out;
}

function Metric({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div role="group" aria-label={label} className="min-w-0 rounded-xl bg-card px-3 py-2 shadow-card">
      <dt className="truncate text-xs text-muted">{label}</dt>
      <dd className="mt-0.5 truncate text-base font-semibold tabular-nums">{children}</dd>
    </div>
  );
}

/** Сообщество: название по главным участникам (если оно среди 50 крупнейших), иначе id. */
function CommunityMetric({ community }: { community: string }) {
  const communities = useCommunities(COMMUNITIES_LIMIT);
  const hit = communities.data?.items.find((c) => c.community === community);
  return (
    <Metric label="Сообщество">
      <RouterAnchor
        href={graphLink({ mode: "community", community })}
        className="text-primary-ink underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-primary"
        title={hit ? communityTitle(hit) : undefined}
      >
        {hit ? communityTitle(hit) : community}
      </RouterAnchor>
    </Metric>
  );
}

/** Вопрос о сущности для чата: заготовка, которую можно поправить перед отправкой. */
export function askAbout(name: string): string {
  return `Что известно о «${name}» и с чем это связано?`;
}

const ACTION = `${GHOST_BUTTON} gap-1.5`;

/** Переходы со страницы сущности: граф, путь, вопрос в чат, страница wiki. */
function Actions({ entity }: { entity: EntityCard }) {
  const id = entity.entity_id;
  return (
    <div role="group" aria-label="Действия" className="flex flex-wrap gap-2">
      <RouterAnchor href={graphLink({ mode: "neighbors", center: id })} className={ACTION}>
        <Network size={15} aria-hidden="true" />
        Открыть в графе
      </RouterAnchor>
      <RouterAnchor href={graphLink({ mode: "path", source: id })} className={ACTION}>
        <Route size={15} aria-hidden="true" />
        Путь до…
      </RouterAnchor>
      <RouterAnchor href={`/entities/compare${stringifyPlainSearch({ a: id })}`} className={ACTION}>
        <ArrowLeftRight size={15} aria-hidden="true" />
        Сравнить с…
      </RouterAnchor>
      <RouterAnchor href={`/chat${stringifyPlainSearch({ ask: askAbout(entity.name), mode: "graph" })}`} className={ACTION}>
        <MessageCircleQuestion size={15} aria-hidden="true" />
        Спросить
      </RouterAnchor>
      {entity.has_page && entity.slug && (
        <RouterAnchor href={`/wiki/${encodeURIComponent(entity.slug)}`} className={ACTION}>
          <BookOpen size={15} aria-hidden="true" />
          Страница wiki
        </RouterAnchor>
      )}
    </div>
  );
}

function Header({ entity }: { entity: EntityCard }) {
  const catalog = useTypeCatalog();
  return (
    <header className="space-y-4">
      <div className="space-y-2">
        <h1 className="text-[2rem] leading-tight font-semibold tracking-tight [overflow-wrap:anywhere]">
          {entity.name}
        </h1>
        <span className="inline-flex items-center gap-1.5 rounded-full bg-card px-2.5 py-1 text-xs font-medium shadow-card">
          <span
            className="size-2 shrink-0 rounded-full"
            style={{ backgroundColor: catalog.typeColor(entity.type) }}
            aria-hidden="true"
          />
          {catalog.typeLabel(entity.type)}
        </span>
        {entity.aliases.length > 0 && (
          <p className="text-sm break-words text-muted">Также: {entity.aliases.join(", ")}</p>
        )}
      </div>
      {entity.description && (
        <p className="max-w-prose text-[15px] leading-relaxed break-words whitespace-pre-wrap">{entity.description}</p>
      )}
      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
        <Metric label="Упоминания">{formatInt(entity.mention_count)}</Metric>
        <Metric label="Связи">{formatInt(entity.degree)}</Metric>
        <Metric label="Документы">{formatInt(entity.documents_count)}</Metric>
        {entity.pagerank !== null && <Metric label="PageRank">{formatScore(entity.pagerank)}</Metric>}
        {entity.community !== null && <CommunityMetric community={entity.community} />}
      </dl>
      <Actions entity={entity} />
    </header>
  );
}

type TabInfo = { key: EntityTab; label: string; count: number | null };

/** Вкладки (WAI-ARIA tabs): стрелки и Home/End переключают сразу. */
function TabList({
  tabs,
  active,
  base,
  onSelect,
}: {
  tabs: TabInfo[];
  active: EntityTab;
  base: string;
  onSelect(tab: EntityTab): void;
}) {
  const refs = useRef(new Map<EntityTab, HTMLButtonElement>());
  const onKeyDown = (event: KeyboardEvent) => {
    const at = tabs.findIndex((t) => t.key === active);
    const next =
      event.key === "ArrowRight"
        ? (at + 1) % tabs.length
        : event.key === "ArrowLeft"
          ? (at - 1 + tabs.length) % tabs.length
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? tabs.length - 1
              : -1;
    if (next < 0) return;
    event.preventDefault();
    const key = tabs[next]!.key;
    onSelect(key);
    refs.current.get(key)?.focus();
  };
  return (
    <div
      role="tablist"
      aria-label="Разделы сущности"
      onKeyDown={onKeyDown}
      className="flex gap-1 overflow-x-auto border-b border-line"
    >
      {tabs.map((t) => {
        const selected = t.key === active;
        return (
          <button
            key={t.key}
            ref={(el) => {
              if (el) refs.current.set(t.key, el);
              else refs.current.delete(t.key);
            }}
            type="button"
            role="tab"
            id={`${base}-tab-${t.key}`}
            aria-selected={selected}
            aria-controls={`${base}-panel-${t.key}`}
            tabIndex={selected ? 0 : -1}
            onClick={() => onSelect(t.key)}
            className={`-mb-px shrink-0 border-b-2 px-3 py-2 text-sm font-medium whitespace-nowrap transition-colors focus-visible:outline-2 focus-visible:outline-primary ${
              selected ? "border-primary text-ink" : "border-transparent text-muted hover:text-ink"
            }`}
          >
            {t.label}
            {t.count !== null && (
              <>
                {" "}
                <span className="text-muted tabular-nums">{formatInt(t.count)}</span>
              </>
            )}
          </button>
        );
      })}
    </div>
  );
}

function WikiTab({ slug }: { slug: string }) {
  const query = useWikiPage({ slug });
  if (query.isError)
    return (
      <FormNote tone="error">
        {query.error instanceof ApiError && query.error.status === 404
          ? "Страница не найдена: возможно, её удалили вместе с документом."
          : errorText(query.error)}
      </FormNote>
    );
  if (!query.data) return <p className="text-muted">Загружаю…</p>;
  return (
    <div className="space-y-6">
      <WikiBody page={query.data} />
      <RouterAnchor href={`/wiki/${encodeURIComponent(slug)}`} className={GHOST_BUTTON}>
        Открыть страницу wiki
      </RouterAnchor>
    </div>
  );
}

function Entity({ id }: { id: string }) {
  const query = useEntity(id);
  const navigate = useNavigate();
  const base = useId();
  const raw = useSearch({ strict: false }) as Record<string, unknown>;
  // Фильтры «Связей» живут здесь: смена вкладки их не теряет, смена id (ключ) — сбрасывает.
  const [filters, setFilters] = useState<RelationFilters>({ direction: "both", predicate: null });
  const entity = query.data;
  const needles = useMemo(() => (entity ? [entity.name, ...entity.aliases] : []), [entity]);

  const selectTab = (tab: EntityTab) =>
    void navigate({
      to: "/entities/$id",
      params: { id },
      search: (prev: Record<string, unknown>) => validateEntitySearch({ ...prev, tab }),
      replace: true,
    });

  const back = (
    <RouterAnchor href="/entities" className={CRUMB}>
      К списку сущностей
    </RouterAnchor>
  );

  let content: ReactNode;
  if (query.isError) {
    const notFound = query.error instanceof ApiError && query.error.status === 404;
    content = (
      <div className="space-y-3">
        <FormNote tone="error">{notFound ? NOT_FOUND : errorText(query.error)}</FormNote>
        {back}
      </div>
    );
  } else if (!entity) {
    content = <p className="text-muted">Загружаю…</p>;
  } else {
    const slug = entity.has_page ? entity.slug : null;
    const tabs: TabInfo[] = [
      { key: "relations", label: "Связи", count: entity.degree },
      { key: "mentions", label: "Упоминания", count: entity.mention_count },
      { key: "documents", label: "Документы", count: entity.documents_count },
      ...(slug ? [{ key: "wiki" as const, label: "Wiki", count: null }] : []),
    ];
    const requested = parseEntityTab(raw.tab);
    const active = tabs.some((t) => t.key === requested) ? requested : "relations";
    content = (
      <>
        <Header entity={entity} />
        <MiniGraph id={id} />
        <div className="space-y-4">
          <TabList tabs={tabs} active={active} base={base} onSelect={selectTab} />
          <section
            role="tabpanel"
            id={`${base}-panel-${active}`}
            aria-labelledby={`${base}-tab-${active}`}
            // Статья wiki — на фоне страницы, как на её собственной странице; списки — в карточке.
            className={active === "wiki" ? "min-w-0 space-y-6" : "min-w-0 rounded-[14px] bg-card p-4 shadow-card sm:p-6"}
          >
            {active === "relations" && <RelationsTab id={id} filters={filters} onFilters={setFilters} />}
            {active === "mentions" && <MentionsTab id={id} needles={needles} />}
            {active === "documents" && <DocumentsTab id={id} />}
            {active === "wiki" && slug && <WikiTab slug={slug} />}
          </section>
        </div>
      </>
    );
  }

  return (
    <div className="mx-auto w-full max-w-4xl space-y-6 pt-2 pb-10 md:pt-6">
      <nav aria-label="Путь">
        <RouterAnchor href="/entities" className={CRUMB}>
          Сущности
        </RouterAnchor>
      </nav>
      {content}
    </div>
  );
}

/** Страница сущности; всё, что зависит от сущности, ключуется её id (раздел не перемонтируется). */
export function EntityPage() {
  const { id } = useParams({ strict: false }) as { id?: string };
  return <Entity key={id} id={id ?? ""} />;
}

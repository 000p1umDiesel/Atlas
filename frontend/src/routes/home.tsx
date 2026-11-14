import { Link } from "@tanstack/react-router";
import { AlertCircle, CheckCircle2, Clock3, FileText, LoaderCircle, Network, RefreshCw } from "lucide-react";
import { useId, useMemo, useState, type ReactNode } from "react";
import { isUnauthorized } from "../api/client";
import { useOverview, useRecentDocuments, useStats, useTypeCatalog } from "../api/queries";
import type { NodeView, RecentDocument } from "../api/types";
import { communityLabel, communitySummaries, type CommunitySummary } from "../graph/insights";
import { buildGraph, DEFAULT_FILTERS } from "../graph/model";
import { serializeGraphSearch } from "../graph/search";
import { StatTile } from "../home/StatTile";
import { TypeBars } from "../home/TypeBars";
import { GlossaryToggle, TypeGlossary } from "../home/TypeGlossary";
import { communityColors, themedCss } from "../lib/communityPalette";
import { countOf, formatDay, formatDayTime } from "../lib/format";
import { RouterAnchor } from "../ui/RouterAnchor";

const TOP_ENTITIES = 8;
const RECENT_DOCUMENTS = 5;

const STATUS: Record<string, { label: string; icon: ReactNode }> = {
  done: { label: "Обработан", icon: <CheckCircle2 size={15} className="text-primary" /> },
  running: { label: "Обрабатывается", icon: <LoaderCircle size={15} className="animate-spin text-primary" /> },
  // queued — загрузка, которую worker ещё не взял; pending — документ registry ждёт обработки
  queued: { label: "В очереди", icon: <Clock3 size={15} className="text-muted" /> },
  pending: { label: "В очереди", icon: <Clock3 size={15} className="text-muted" /> },
  failed: { label: "Ошибка", icon: <AlertCircle size={15} className="text-ink" /> },
};

function SectionTitle({ children, aside }: { children: ReactNode; aside?: ReactNode }) {
  return (
    <div className="mb-4 flex items-baseline justify-between gap-3">
      <h2 className="text-xl font-semibold tracking-tight">{children}</h2>
      {aside}
    </div>
  );
}

const QUIET_LINK = "text-sm text-primary-ink underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-primary";

function Unavailable({ error }: { error: unknown }) {
  return (
    <div className="mx-auto max-w-xl rounded-[14px] bg-card p-8 shadow-card">
      <h2 className="text-xl font-semibold">Сводка пока недоступна</h2>
      <p className="mt-2 text-muted">
        {error instanceof Error ? error.message : "Нет данных"}.
        {/* Без сессии совет не нужен: 401 уводит на страницу входа. */}
        {isUnauthorized(error) ? "" : " Обновите страницу чуть позже."}
      </p>
    </div>
  );
}

/** Главная: сводка базы знаний на одном листе. */
export function HomePage() {
  const stats = useStats();
  const overview = useOverview();
  const catalog = useTypeCatalog();
  const recent = useRecentDocuments();
  const [glossaryOpen, setGlossaryOpen] = useState(false);
  const glossaryId = useId();

  const graph = useMemo(() => (overview.data ? buildGraph(overview.data.graph) : null), [overview.data]);
  const communities = useMemo(() => (graph ? communitySummaries(graph) : []), [graph]);
  const top = useMemo<NodeView[]>(
    () =>
      overview.data
        ? [...overview.data.graph.nodes]
            .sort((a, b) => (b.pagerank ?? 0) - (a.pagerank ?? 0) || b.mention_count - a.mention_count)
            .slice(0, TOP_ENTITIES)
        : [],
    [overview.data],
  );

  const s = stats.data;
  const leadType = [...catalog.types].sort((a, b) => b.count - a.count)[0];

  return (
    // Во всю ширину до 1800px: на широком экране сводка раскладывается в три колонки.
    <div className="mx-auto w-full max-w-[1800px] pt-2 pb-10 md:pt-6">
      <header className="max-w-2xl">
        <h1 className="text-[2.25rem] leading-tight font-semibold tracking-tight">Главная</h1>
        {leadType && top[0] && (
          <p className="mt-2 font-serif text-lg leading-relaxed text-muted">
            Больше всего в базе сущностей типа «{catalog.typeLabel(leadType.type)}», а в центре графа — {top[0].name}.
          </p>
        )}
      </header>

      {stats.isError ? (
        <div className="mt-8">
          <Unavailable error={stats.error} />
        </div>
      ) : (
        <>
          {/* Одна карточка, ячейки разделены волосяными линиями (зазор 1px поверх цвета линии). */}
          <div className="mt-8 grid grid-cols-2 gap-px overflow-hidden rounded-[14px] bg-line shadow-card lg:grid-cols-4">
            <StatTile index={0} label="Документы" value={s?.documents ?? null} note={s ? countOf(s.chunks, ["фрагмент", "фрагмента", "фрагментов"]) : undefined} />
            <StatTile index={1} label="Сущности" value={s?.entities ?? null} note={catalog.order.length ? countOf(catalog.order.length, ["тип", "типа", "типов"]) : undefined} />
            <StatTile index={2} label="Связи" value={s?.relations ?? null} note={communities.length ? countOf(communities.length, ["сообщество", "сообщества", "сообществ"]) : undefined} />
            <StatTile index={3} label="Страницы wiki" value={s?.wiki_pages ?? null} note={s && s.entities > 0 ? `у ${Math.round((s.wiki_pages / s.entities) * 100)}% сущностей` : undefined} />
          </div>

          <div className="mt-12 grid grid-cols-1 gap-x-10 gap-y-12 lg:grid-cols-12 2xl:gap-x-12">
            <section className="lg:col-span-7 2xl:col-span-5" aria-labelledby="home-types">
              <SectionTitle aside={<Link to="/graph" className={QUIET_LINK}>Открыть граф</Link>}>
                <span id="home-types">Сущности по типам</span>
              </SectionTitle>
              <div className="rounded-[14px] bg-card p-4 shadow-card sm:p-5">
                {catalog.types.length > 0 || overview.isSuccess ? (
                  <>
                    <TypeBars catalog={catalog} />
                    {catalog.types.length > 0 && (
                      <div className="-mx-2 mt-3 border-t border-line pt-3">
                        <GlossaryToggle open={glossaryOpen} controls={glossaryId} onToggle={() => setGlossaryOpen((v) => !v)} />
                      </div>
                    )}
                  </>
                ) : (
                  <p className="text-sm text-muted">Загружаем…</p>
                )}
              </div>
            </section>

            {/* Справочник — во всю ширину сетки сразу под первым рядом (на узком экране — под полосами). */}
            <TypeGlossary id={glossaryId} open={glossaryOpen} catalog={catalog} className="lg:order-1 lg:col-span-12" />

            {/* 18 полос выше карточки анализа: она не растягивается на всю высоту ряда. */}
            <section className="lg:col-span-5 lg:self-start 2xl:col-span-4" aria-labelledby="home-analysis">
              <AnalysisCard
                ready={s?.analysis.ready ?? null}
                completedAt={s?.analysis.completed_at ?? null}
                communities={communities}
              />
            </section>

            <section className="lg:order-2 lg:col-span-7 2xl:order-last 2xl:col-span-12" aria-labelledby="home-top">
              <SectionTitle>
                <span id="home-top">Самые важные сущности</span>
              </SectionTitle>
              <ol aria-label="Самые важные сущности" className="grid gap-x-6 sm:grid-cols-2 2xl:grid-cols-4">
                {top.map((node, i) => (
                  <li key={node.entity_id} className="border-b border-line">
                    <Link
                      to="/graph"
                      search={serializeGraphSearch({ ...DEFAULT_FILTERS, highlight: node.name, view: "2d" })}
                      className="group flex items-center gap-3 py-3 focus-visible:outline-2 focus-visible:outline-primary"
                    >
                      <span className="w-5 shrink-0 text-right font-serif text-sm text-muted tabular-nums" aria-hidden="true">
                        {i + 1}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate font-medium underline-offset-4 group-hover:underline">{node.name}</span>
                        <span className="mt-0.5 flex items-center gap-1.5 text-xs text-muted">
                          <span className="size-2 rounded-full" style={{ backgroundColor: catalog.typeColor(node.type) }} aria-hidden="true" />
                          <span>
                            {catalog.typeLabel(node.type)}, {countOf(node.mention_count, ["упоминание", "упоминания", "упоминаний"])}
                          </span>
                        </span>
                      </span>
                    </Link>
                  </li>
                ))}
              </ol>
            </section>

            <section className="lg:order-2 lg:col-span-5 2xl:order-none 2xl:col-span-3" aria-labelledby="home-recent">
              <SectionTitle aside={<Link to="/documents" className={QUIET_LINK}>Все документы</Link>}>
                <span id="home-recent">Последние документы</span>
              </SectionTitle>
              <RecentList docs={recent.data?.slice(0, RECENT_DOCUMENTS) ?? []} />
            </section>
          </div>
        </>
      )}
    </div>
  );
}

export function AnalysisCard({
  ready,
  completedAt,
  communities,
}: {
  ready: boolean | null;
  completedAt: string | null;
  communities: CommunitySummary[];
}) {
  const largest = communities.slice(0, 4);
  const colors = communityColors(communities.map((c) => c.id));
  // Не готов, но запуск был — результат устарел (граф изменился после анализа).
  const stale = ready === false && completedAt !== null;
  return (
    <div className="flex h-full flex-col rounded-[14px] border border-primary/25 bg-primary/10 p-6">
      <div className="flex items-start gap-4">
        <span className="grid size-11 shrink-0 place-items-center rounded-[14px] bg-card text-primary-ink shadow-card">
          <Network size={22} strokeWidth={1.8} />
        </span>
        <div className="min-w-0">
          <h2 id="home-analysis" className="text-xl font-semibold tracking-tight">
            Анализ графа
          </h2>
          {ready === null ? (
            <p className="mt-1 text-sm text-muted">Загружаем…</p>
          ) : (
            <p className="mt-1 flex items-center gap-1.5 text-sm">
              {ready ? <CheckCircle2 size={15} className="text-primary-ink" /> : <Clock3 size={15} className="text-muted" />}
              <span className="font-medium">{ready ? "Готов" : stale ? "Устарел — граф изменился после анализа" : "Не запускался"}</span>
            </p>
          )}
          {completedAt && (ready || stale) && (
            <p className="mt-0.5 text-xs text-muted">{ready ? "Посчитан" : "Последний запуск"} {formatDayTime(completedAt)}</p>
          )}
        </div>
      </div>
      <p className="mt-5 max-w-prose text-sm leading-relaxed text-muted">
        {ready
          ? `PageRank показывает, какие сущности держат граф, а ${communities.length > 0 ? countOf(communities.length, ["сообщество", "сообщества", "сообществ"]) : "сообщества"} собирают тесно связанные темы. По ним граф раскрашен и упорядочен.`
          : stale
            ? "Результаты анализа устарели: граф упорядочен по числу упоминаний, пока анализ не будет запущен заново."
            : "PageRank и сообщества ещё не посчитаны: граф упорядочен по числу упоминаний."}
      </p>
      {ready && largest.length > 0 && (
        <ul aria-label="Крупнейшие сообщества" className="mt-5 space-y-2 text-sm">
          {largest.map((c) => (
            <li key={c.id}>
              <Link
                to="/graph"
                search={serializeGraphSearch({ ...DEFAULT_FILTERS, community: c.id, view: "2d" })}
                className="flex items-center gap-2.5 rounded-lg underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-primary"
              >
                <span className="size-2.5 shrink-0 rounded-full" style={{ backgroundColor: themedCss(colors.get(c.id)) }} aria-hidden="true" />
                <span className="min-w-0 flex-1 truncate">{communityLabel(c)}</span>
                <span className="shrink-0 text-xs text-muted tabular-nums">{c.size}</span>
              </Link>
            </li>
          ))}
        </ul>
      )}
      <div className="mt-auto flex flex-wrap gap-2 pt-6">
        <Link
          to="/graph"
          className="inline-flex items-center gap-2 rounded-xl bg-primary-deep px-4 py-2 text-sm text-on-primary shadow-card transition-opacity hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
        >
          Открыть граф
        </Link>
        <button
          type="button"
          disabled
          title="Запуск анализа появится после этапа 2"
          className="inline-flex items-center gap-2 rounded-xl border border-line bg-card/60 px-4 py-2 text-sm text-muted disabled:cursor-not-allowed"
        >
          <RefreshCw size={15} />
          Пересчитать
        </button>
      </div>
    </div>
  );
}

function RecentList({ docs }: { docs: RecentDocument[] }) {
  if (docs.length === 0) return <p className="text-sm text-muted">Документов пока нет.</p>;
  return (
    <ul aria-label="Последние документы" className="rounded-[14px] bg-card px-4 shadow-card">
      {docs.map((doc) => {
        const status = STATUS[doc.status] ?? { label: doc.status, icon: <Clock3 size={15} className="text-muted" /> };
        return (
          <li key={doc.id} className="flex items-center gap-3 border-b border-line py-3 last:border-b-0">
            <FileText size={18} strokeWidth={1.7} className="shrink-0 text-muted" aria-hidden="true" />
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium" title={doc.name}>
                <RouterAnchor
                  href={`/documents/${encodeURIComponent(doc.id)}`}
                  className="underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-primary"
                >
                  {doc.name}
                </RouterAnchor>
              </p>
              {/* null — загрузка ещё ждёт worker'а */}
              <p className="mt-0.5 text-xs text-muted">{doc.updated_at ? formatDay(doc.updated_at) : "в очереди"}</p>
            </div>
            <span className="flex shrink-0 items-center gap-1.5 text-xs text-muted">
              {status.icon}
              {status.label}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

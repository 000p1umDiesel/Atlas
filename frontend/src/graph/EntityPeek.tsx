import { ArrowLeft, ArrowRight, ExternalLink, Users } from "lucide-react";
import { useId, useMemo, type ReactNode } from "react";
import { communityColorVar, typeColorVar } from "../lib/entityColors";
import { formatInt, formatScore } from "../lib/format";
import { communityLabel, topRelations, type CommunitySummary } from "./insights";
import type { Graph } from "./model";

type Props = {
  graph: Graph;
  entityId: string;
  entityTypes: string[];
  communities: CommunitySummary[];
  rank: Map<string, number>;
  onSelect(entityId: string): void;
  onShowCommunity(community: string): void;
};

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div role="group" aria-label={label} className="min-w-0 rounded-xl bg-bg/70 px-3 py-2.5">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="mt-0.5 truncate font-medium tabular-nums">{children}</dd>
    </div>
  );
}

/** Краткая карточка сущности в выезжающей панели. */
export function EntityPeek({ graph, entityId, entityTypes, communities, rank, onSelect, onShowCommunity }: Props) {
  const pageHint = useId();
  const relations = useMemo(() => topRelations(graph, entityId, 8), [graph, entityId]);
  if (!graph.hasNode(entityId)) return <p className="text-muted">Сущность не найдена в текущем графе.</p>;
  const a = graph.getNodeAttributes(entityId);
  const community = a.community === null ? null : communities.find((c) => c.id === a.community) ?? null;
  const degree = graph.degree(entityId);

  return (
    <div className="space-y-6">
      <span className="inline-flex items-center gap-2 rounded-full bg-bg px-3 py-1 text-sm">
        <span className="size-2.5 rounded-full" style={{ backgroundColor: typeColorVar(a.type, entityTypes) }} aria-hidden="true" />
        {a.type}
      </span>

      <dl className="grid grid-cols-2 gap-2">
        <Fact label="Упоминания">{formatInt(a.mention_count)}</Fact>
        <Fact label="PageRank">
          {a.pagerank === null ? "нет анализа" : formatScore(a.pagerank)}
          {a.pagerank !== null && rank.has(entityId) && (
            <span className="ml-1.5 text-xs font-normal text-muted">
              {formatInt(rank.get(entityId)!)}-е место
            </span>
          )}
        </Fact>
        <Fact label="Связи">{formatInt(degree)}</Fact>
        <Fact label="Сообщество">
          {community ? (
            <span className="inline-flex max-w-full items-center gap-1.5">
              <span className="size-2 shrink-0 rounded-full" style={{ backgroundColor: communityColorVar(community.id) }} aria-hidden="true" />
              <span className="truncate">{formatInt(community.size)} участников</span>
            </span>
          ) : (
            "нет"
          )}
        </Fact>
      </dl>

      <section className="space-y-2">
        <h3 className="text-base font-semibold">Самые весомые связи</h3>
        {relations.length === 0 ? (
          <p className="text-sm text-muted">Связей нет.</p>
        ) : (
          <ul aria-label="Связи" className="-mx-2">
            {relations.map((r) => (
              <li key={r.key}>
                <button
                  type="button"
                  onClick={() => onSelect(r.otherId)}
                  className="group flex w-full items-center gap-3 rounded-xl px-2 py-2 text-left transition-colors hover:bg-bg focus-visible:outline-2 focus-visible:outline-primary"
                >
                  <span className="grid size-7 shrink-0 place-items-center rounded-full bg-bg text-muted group-hover:bg-card">
                    {r.direction === "out" ? <ArrowRight size={14} aria-label="исходящая" /> : <ArrowLeft size={14} aria-label="входящая" />}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-medium">{r.otherName}</span>
                    <span className="block truncate text-xs text-muted">{r.predicate.replaceAll("_", " ")}</span>
                  </span>
                  <span className="shrink-0 text-xs text-muted tabular-nums" title="Вес связи">
                    {formatScore(r.weight)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <div className="flex flex-wrap gap-2 border-t border-line pt-4">
        {community && (
          <button
            type="button"
            onClick={() => onShowCommunity(community.id)}
            title={communityLabel(community)}
            className="inline-flex items-center gap-2 rounded-xl bg-primary-deep px-3.5 py-2 text-sm text-on-primary shadow-card transition-opacity hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
          >
            <Users size={15} />
            Показать сообщество
          </button>
        )}
        <button
          type="button"
          disabled
          aria-describedby={pageHint}
          className="inline-flex items-center gap-2 rounded-xl border border-line px-3.5 py-2 text-sm text-muted disabled:cursor-not-allowed"
        >
          <ExternalLink size={15} />
          Открыть страницу
        </button>
        <p id={pageHint} className="w-full text-xs text-muted">
          Страница сущности появится на этапе 4.
        </p>
      </div>
    </div>
  );
}

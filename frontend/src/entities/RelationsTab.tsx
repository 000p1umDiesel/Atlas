import { ArrowLeft, ArrowRight } from "lucide-react";
import { useMemo } from "react";
import { useEntityRelations, type RelationDirection } from "../api/entities";
import { useTypeCatalog } from "../api/queries";
import type { EntityRelation } from "../api/types";
import { NAME_LINK } from "../ui/styles";
import { predicateLabel } from "../graph/insights";
import { formatInt, formatScore } from "../lib/format";
import type { TypeCatalog } from "../lib/typeCatalog";
import { usePanel } from "../shell/panel";
import { FormNote } from "../ui/form";
import { RouterAnchor } from "../ui/RouterAnchor";
import { Segmented } from "../ui/Segmented";
import { entityHref } from "./EntityTable";
import { errorText, LoadMore } from "./parts";

const DIRECTIONS: { value: RelationDirection; label: string }[] = [
  { value: "both", label: "Все" },
  { value: "out", label: "Исходящие" },
  { value: "in", label: "Входящие" },
];
const CHIP =
  "inline-flex items-center gap-1.5 rounded-full border border-line bg-card px-2.5 py-1 text-[13px] transition-colors hover:border-primary/60 focus-visible:outline-2 focus-visible:outline-primary aria-pressed:border-primary aria-pressed:bg-primary/15 aria-pressed:text-primary-ink";
const EVIDENCE =
  "rounded-md px-1.5 py-0.5 text-xs text-primary-ink underline-offset-4 hover:bg-ink/[0.06] hover:underline focus-visible:outline-2 focus-visible:outline-primary";

function RelationRow({ relation: r, id, catalog }: { relation: EntityRelation; id: string; catalog: TypeCatalog }) {
  const { open } = usePanel();
  // Петля (сама на себя) считается исходящей.
  const out = r.src_id === id;
  const other = out
    ? { id: r.dst_id, name: r.dst_name, type: r.dst_type }
    : { id: r.src_id, name: r.src_name, type: r.src_type };
  const Arrow = out ? ArrowRight : ArrowLeft;
  return (
    <li className="border-b border-line py-3 last:border-b-0">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <Arrow size={15} aria-hidden="true" className="shrink-0 text-muted" />
        <span className="sr-only">{out ? "Исходящая связь" : "Входящая связь"}</span>
        <span className="text-sm text-muted">{predicateLabel(r.predicate)}</span>
        <span className="inline-flex min-w-0 items-center gap-1.5">
          <span
            className="size-2.5 shrink-0 rounded-full"
            style={{ backgroundColor: catalog.typeColor(other.type) }}
            title={catalog.typeLabel(other.type)}
            aria-hidden="true"
          />
          <RouterAnchor href={entityHref(other.id)} className={NAME_LINK}>
            {other.name}
          </RouterAnchor>
        </span>
        <span className="ml-auto text-xs text-muted tabular-nums">вес {formatScore(r.weight)}</span>
      </div>
      {r.description && (
        <p className="mt-1 text-sm leading-relaxed break-words whitespace-pre-wrap">{r.description}</p>
      )}
      {r.evidence.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1">
          {r.evidence.map((chunk, i) => (
            <button key={chunk} type="button" onClick={() => open({ kind: "chunk", id: chunk })} className={EVIDENCE}>
              фрагмент {i + 1}
            </button>
          ))}
        </div>
      )}
    </li>
  );
}

/** Фильтры вкладки «Связи»: их держит страница (ключ — id сущности), смена вкладки их не теряет. */
export interface RelationFilters {
  direction: RelationDirection;
  predicate: string | null;
}

/** Связи сущности: направление, фильтр по предикату (повторный клик снимает), страницы по 50. */
export function RelationsTab({
  id,
  filters,
  onFilters,
}: {
  id: string;
  filters: RelationFilters;
  onFilters(next: RelationFilters): void;
}) {
  const catalog = useTypeCatalog();
  const { direction, predicate } = filters;
  const query = useEntityRelations(id, direction, predicate);
  const pages = query.data?.pages;
  const items = useMemo(() => pages?.flatMap((p) => p.items) ?? [], [pages]);
  const total = pages?.[0]?.total ?? 0;
  const facet = pages?.[0]?.predicates ?? [];
  // Выбранный предикат остаётся чипом, даже если в новом направлении его нет: его можно снять.
  const chips =
    predicate !== null && !facet.some((f) => f.predicate === predicate) ? [...facet, { predicate, count: 0 }] : facet;
  const canLoadMore = query.hasNextPage && !query.isFetchingNextPage && !query.isPlaceholderData;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="w-full max-w-sm">
          <Segmented
            label="Направление связей"
            options={DIRECTIONS}
            value={direction}
            onChange={(value) => onFilters({ ...filters, direction: value })}
            layoutId="entity-relations-direction"
            size="sm"
          />
        </div>
      </div>
      {chips.length > 0 && (
        <ul aria-label="Предикаты" className="flex flex-wrap gap-1.5">
          {chips.map((f) => (
            <li key={f.predicate}>
              <button
                type="button"
                aria-pressed={predicate === f.predicate}
                onClick={() => onFilters({ ...filters, predicate: predicate === f.predicate ? null : f.predicate })}
                className={CHIP}
              >
                {predicateLabel(f.predicate)} <span className="text-muted tabular-nums">{formatInt(f.count)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {query.isError && <FormNote tone="error">{errorText(query.error)}</FormNote>}
      {query.data === undefined ? (
        query.isPending && <p className="text-muted">Загружаю…</p>
      ) : items.length === 0 ? (
        <p className="text-muted">Связей нет.</p>
      ) : (
        <div
          className={`transition-opacity ${query.isPlaceholderData ? "opacity-60" : ""}`}
          aria-busy={query.isPlaceholderData || query.isFetchingNextPage}
        >
          <ul aria-label="Связи">
            {items.map((r, i) => (
              <RelationRow key={`${r.src_id}|${r.predicate}|${r.dst_id}|${i}`} relation={r} id={id} catalog={catalog} />
            ))}
          </ul>
          {query.hasNextPage && (
            <LoadMore shown={items.length} total={total} disabled={!canLoadMore} onClick={() => void query.fetchNextPage()} />
          )}
        </div>
      )}
    </div>
  );
}

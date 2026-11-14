import { BookOpen, IdCard, Network, Users } from "lucide-react";
import { useEntity } from "../api/entities";
import { useTypeCatalog } from "../api/queries";
import { graphLink } from "../graph/search";
import { formatInt, formatScore } from "../lib/format";
import { RouterAnchor } from "../ui/RouterAnchor";
import { PANEL_ACTION, PanelError, PanelLoading } from "./common";

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div role="group" aria-label={label} className="min-w-0 rounded-xl bg-bg px-3 py-2">
      <dt className="truncate text-xs text-muted">{label}</dt>
      <dd className="mt-0.5 truncate text-base font-semibold tabular-nums">{value}</dd>
    </div>
  );
}

/** Карточка сущности: тип, псевдонимы, описание, числа и место в анализе графа. */
export function EntityPanel({ id }: { id: string }) {
  const catalog = useTypeCatalog();
  const query = useEntity(id);
  if (query.isPending) return <PanelLoading />;
  if (query.isError) return <PanelError error={query.error} notFound="Сущность не найдена" />;
  const entity = query.data;
  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-xl font-semibold break-words">{entity.name}</h3>
        <span className="mt-2 inline-flex items-center gap-1.5 rounded-full bg-bg px-2.5 py-1 text-xs font-medium">
          <span
            className="size-2 shrink-0 rounded-full"
            style={{ backgroundColor: catalog.typeColor(entity.type) }}
            aria-hidden="true"
          />
          {catalog.typeLabel(entity.type)}
        </span>
        {entity.aliases.length > 0 && (
          <p className="mt-2 text-sm break-words text-muted">Также: {entity.aliases.join(", ")}</p>
        )}
      </div>
      {entity.description && (
        <p className="text-[15px] leading-relaxed break-words whitespace-pre-wrap">{entity.description}</p>
      )}
      <dl className="grid grid-cols-3 gap-2">
        <Metric label="Упоминания" value={formatInt(entity.mention_count)} />
        <Metric label="Связи" value={formatInt(entity.degree)} />
        <Metric label="Документы" value={formatInt(entity.documents_count)} />
        {entity.pagerank !== null && <Metric label="PageRank" value={formatScore(entity.pagerank)} />}
      </dl>
      <div className="flex flex-wrap gap-2">
        <RouterAnchor href={`/entities/${encodeURIComponent(entity.entity_id)}`} className={PANEL_ACTION}>
          <IdCard size={15} aria-hidden="true" />
          Страница сущности
        </RouterAnchor>
        <RouterAnchor href={graphLink({ mode: "neighbors", center: entity.entity_id })} className={PANEL_ACTION}>
          <Network size={15} aria-hidden="true" />
          Окрестность на графе
        </RouterAnchor>
        {entity.has_page && entity.slug && (
          <RouterAnchor href={`/wiki/${encodeURIComponent(entity.slug)}`} className={PANEL_ACTION}>
            <BookOpen size={15} aria-hidden="true" />
            Страница wiki
          </RouterAnchor>
        )}
        {entity.community !== null && (
          <RouterAnchor href={graphLink({ mode: "community", community: entity.community })} className={PANEL_ACTION}>
            <Users size={15} aria-hidden="true" />
            Сообщество на графе
          </RouterAnchor>
        )}
      </div>
    </div>
  );
}

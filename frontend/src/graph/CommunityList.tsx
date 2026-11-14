import { LoaderCircle, Play } from "lucide-react";
import { ApiError } from "../api/client";
import { communityTitle, useCommunities } from "../api/entities";
import { useTypeCatalog } from "../api/queries";
import { analysisActive, useAnalysis, useStartAnalysis } from "../api/status";
import type { CommunityView } from "../api/types";
import { countOf } from "../lib/format";
import type { TypeCatalog } from "../lib/typeCatalog";

/** Сообществ в списке режима: самые крупные. */
export const COMMUNITIES_LIMIT = 50;
const MEMBERS: [string, string, string] = ["участник", "участника", "участников"];

/** Состав сообщества по типам: доли полосы — числа по типам, цвета — каталога. */
function TypeStrip({ types, catalog }: { types: CommunityView["types"]; catalog: TypeCatalog }) {
  const rows = Object.entries(types)
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  const total = rows.reduce((s, [, n]) => s + n, 0);
  if (total === 0) return null;
  return (
    <span className="mt-1.5 flex h-1.5 overflow-hidden rounded-full bg-ink/[0.06]" aria-hidden="true">
      {rows.map(([type, n]) => (
        <span key={type} className="h-full" style={{ width: `${(n / total) * 100}%`, backgroundColor: catalog.typeColor(type) }} />
      ))}
    </span>
  );
}

/** Подпись состава для чтения с экрана: «Человек 10, Понятие 2». */
function typesText(types: CommunityView["types"], catalog: TypeCatalog): string {
  return Object.entries(types)
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([type, n]) => `${catalog.typeLabel(type)} ${n}`)
    .join(", ");
}

/** Анализ графа не запускался или устарел: объяснение и запуск. */
function NoAnalysis() {
  const analysis = useAnalysis();
  const start = useStartAnalysis();
  const running = analysisActive(analysis.data) || start.isPending;
  return (
    <div className="space-y-3 px-1">
      <p className="text-sm text-muted">Сообщества появятся после анализа графа.</p>
      <button
        type="button"
        disabled={running}
        onClick={() => start.mutate()}
        className="inline-flex items-center gap-1.5 rounded-xl bg-primary-deep px-3 py-1.5 text-[13px] font-medium text-on-primary shadow-card transition-opacity hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary disabled:cursor-default disabled:opacity-60"
      >
        {running ? <LoaderCircle size={14} className="animate-spin motion-reduce:animate-none" /> : <Play size={14} />}
        {running ? "Анализ идёт…" : "Запустить анализ"}
      </button>
      {start.isError && <p className="text-sm text-danger">{start.error instanceof ApiError ? start.error.detail : "Ошибка запроса"}</p>}
    </div>
  );
}

/** Сообщества актуального анализа по размеру; клик выбирает сообщество для графа. */
export function CommunityList({ selected, onSelect }: { selected: string | null; onSelect(community: string): void }) {
  const communities = useCommunities(COMMUNITIES_LIMIT);
  const catalog = useTypeCatalog();

  if (communities.isPending) return <p className="px-1 text-sm text-muted">Загружаю…</p>;
  if (communities.isError) {
    return (
      <p className="px-1 text-sm text-danger">
        {communities.error instanceof ApiError ? communities.error.detail : "Ошибка запроса"}
      </p>
    );
  }
  if (!communities.data.ready) return <NoAnalysis />;
  if (communities.data.items.length === 0) return <p className="px-1 text-sm text-muted">Сообществ нет.</p>;

  return (
    <ul aria-label="Сообщества" className="space-y-0.5">
      {communities.data.items.map((c) => {
        const current = c.community === selected;
        const title = communityTitle(c);
        return (
          <li key={c.community}>
            <button
              type="button"
              aria-current={current ? "true" : undefined}
              onClick={() => onSelect(c.community)}
              title={title}
              className="w-full rounded-lg px-2.5 py-2 text-left transition-colors hover:bg-ink/[0.06] focus-visible:outline-2 focus-visible:outline-primary aria-[current=true]:bg-primary/15"
            >
              <span className="flex items-baseline gap-2">
                <span className={`min-w-0 flex-1 truncate text-sm ${current ? "font-medium text-primary-ink" : ""}`}>{title}</span>
                <span className="shrink-0 text-xs text-muted tabular-nums">{countOf(c.size, MEMBERS)}</span>
              </span>
              <TypeStrip types={c.types} catalog={catalog} />
              {Object.keys(c.types).length > 0 && <span className="sr-only">, {typesText(c.types, catalog)}</span>}
            </button>
          </li>
        );
      })}
    </ul>
  );
}

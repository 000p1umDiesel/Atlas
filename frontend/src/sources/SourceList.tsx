import type { ReactNode } from "react";
import type { Source } from "../api/types";
import { KIND_LABELS, labelOf } from "../lib/labels";
import { usePanel, type PanelRef } from "../shell/panel";
import { RouterAnchor } from "../ui/RouterAnchor";

/** Что открыть по источнику: фрагмент или сущность — в правой панели, wiki — страницу. */
export function sourceAction(source: Source): { panel: PanelRef } | { href: string } | null {
  if (source.kind === "chunk") return { panel: { kind: "chunk", id: source.ref } };
  if (source.kind === "entity") return { panel: { kind: "entity", id: source.ref } };
  if (source.kind === "wiki") return { href: `/wiki/page/${encodeURIComponent(source.ref.split("#")[0]!)}` };
  return null; // связь: всё видно в самой строке
}

/** Имя источника для списка. */
export function sourceName(source: Source): string {
  if (source.kind === "chunk") return source.path || "документ";
  if (source.kind === "wiki") return source.path ? source.path.replace(/\.md$/, "") : "страница wiki";
  return source.snippet ? source.snippet.split("\n")[0]!.slice(0, 80) : labelOf(KIND_LABELS, source.kind);
}

/** «документ, стр. 4, удалён». */
function sourceMeta(source: Source): string {
  return (
    labelOf(KIND_LABELS, source.kind) +
    (source.page !== null && source.page !== undefined ? `, стр. ${source.page}` : "") +
    (source.available === false ? ", удалён" : "")
  );
}

const ROW =
  "grid w-full grid-cols-[auto_minmax(0,1fr)] items-baseline gap-x-2.5 rounded-lg px-2 py-1.5 text-left text-sm";
const ACTIVE = `${ROW} transition-colors hover:bg-ink/[0.06] focus-visible:outline-2 focus-visible:outline-primary`;

function SourceItem({ source, onPanel }: { source: Source; onPanel(ref: PanelRef): void }) {
  const name = sourceName(source);
  const content: ReactNode = (
    <>
      <span
        className={`grid h-5 min-w-5 place-items-center rounded-md px-1 text-xs font-semibold tabular-nums ${
          source.cited ? "bg-primary/20 text-primary-ink" : "bg-bg text-muted"
        }`}
      >
        {source.n}
      </span>
      <span className="min-w-0 truncate font-medium">{name}</span>
      <span className="col-start-2 truncate text-xs text-muted">{sourceMeta(source)}</span>
    </>
  );
  const action = sourceAction(source);
  const title = source.snippet || undefined;
  let body: ReactNode;
  if (action === null) {
    // Связь: открывать нечего — сниппет (если он длиннее имени) виден прямо в строке.
    body = (
      <div className={ROW} title={title}>
        {content}
        {source.snippet && source.snippet !== name && (
          <span className="col-start-2 text-xs break-words whitespace-pre-wrap text-muted">{source.snippet}</span>
        )}
      </div>
    );
  } else if ("href" in action) {
    body = (
      <RouterAnchor href={action.href} className={ACTIVE} title={title}>
        {content}
      </RouterAnchor>
    );
  } else {
    body = (
      <button type="button" className={ACTIVE} title={title} onClick={() => onPanel(action.panel)}>
        {content}
      </button>
    );
  }
  return <li>{body}</li>;
}

/**
 * Источники ответа: те, на которые сослался ответ, — списком; остальные из контекста —
 * под раскрывающимся «Ещё N…». Без источников ничего не рисуется.
 */
export function SourceList({ sources }: { sources: Source[] }) {
  const { open } = usePanel();
  if (sources.length === 0) return null;
  const cited = sources.filter((s) => s.cited);
  const rest = sources.filter((s) => !s.cited);
  const list = (items: Source[]) => (
    <ol className="space-y-0.5">
      {items.map((s) => (
        <SourceItem key={`${s.n}:${s.kind}:${s.ref}`} source={s} onPanel={open} />
      ))}
    </ol>
  );
  return (
    <div className="mt-3 space-y-1">
      {cited.length > 0 && list(cited)}
      {rest.length > 0 && (
        <details className="group">
          <summary className="cursor-pointer rounded-lg px-2 py-1 text-sm text-muted transition-colors select-none hover:text-ink focus-visible:outline-2 focus-visible:outline-primary">
            {cited.length > 0 ? `Ещё ${rest.length} в контексте, без ссылок в ответе` : `Источники: ${rest.length}`}
          </summary>
          {list(rest)}
        </details>
      )}
    </div>
  );
}

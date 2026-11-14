import { useAnalysis, useStartAnalysis, analysisActive } from "../api/status";
import { formatDayTime, formatInt } from "../lib/format";
import { messageOf } from "../lib/errors";
import { usePanel } from "../shell/panel";
import { GHOST_BUTTON } from "../ui/styles";
import { StatusBadge } from "../ui/StatusBadge";
import { FormNote } from "../ui/form";
import { Block } from "./parts";

const TOP = 10;

/** «Анализ графа»: запуск, строка задачи, готовый снимок и самые связанные сущности. */
export function Analysis() {
  const query = useAnalysis();
  const start = useStartAnalysis();
  const panel = usePanel();
  const state = query.data;
  const job = state?.job ?? null;
  const active = analysisActive(state) || start.isPending;
  const meta = state?.metadata ?? {};
  const error = start.error ?? query.error;

  return (
    <Block
      title="Анализ графа"
      action={
        <button type="button" className={GHOST_BUTTON} disabled={active} onClick={() => start.mutate()}>
          {active ? "Анализ идёт…" : "Запустить анализ графа"}
        </button>
      }
    >
      <p className="text-sm text-muted">
        PageRank находит самые связанные сущности, Leiden делит граф на сообщества. Ответы в режимах «Граф» и «Всё вместе»
        используют последний готовый снимок.
      </p>
      {error && (
        <div className="mt-3">
          <FormNote tone="error">{messageOf(error)}</FormNote>
        </div>
      )}
      {job && (
        <p className="mt-3 flex flex-wrap items-center gap-2 text-sm">
          <span>Задача:</span>
          <StatusBadge value={job.status} />
          {job.status === "running" && job.progress && <span>{job.progress}</span>}
          {job.status === "failed" && (
            <span className="text-danger [overflow-wrap:anywhere]">{(job.error ?? "").replace(/^AnalysisError: /, "")}</span>
          )}
        </p>
      )}
      {!state && !query.error && <p className="mt-3 text-sm text-muted">Загружаю…</p>}
      {state &&
        (state.ready ? (
          <div className="mt-4">
            <p>
              Снимок от {state.completed_at ? formatDayTime(state.completed_at) : "—"}: {formatInt(meta.nodes ?? 0)} сущностей,{" "}
              {formatInt(meta.relationships ?? 0)} связей, {formatInt(meta.communities ?? 0)} сообществ.
            </p>
            {state.top.length > 0 && (
              <>
                <h3 className="mt-4 mb-2 font-medium">Самые связанные</h3>
                <ol className="list-decimal space-y-1 pl-6 text-sm marker:text-muted">
                  {state.top.slice(0, TOP).map((e) => (
                    <li key={e.entity_id}>
                      <button
                        type="button"
                        onClick={() => panel.open({ kind: "entity", id: e.entity_id })}
                        className="text-left font-medium underline-offset-4 hover:text-primary-ink hover:underline focus-visible:outline-2 focus-visible:outline-primary"
                      >
                        {e.name}
                      </button>{" "}
                      <span className="text-xs text-muted tabular-nums">{e.pagerank.toFixed(3)}</span>
                    </li>
                  ))}
                </ol>
              </>
            )}
          </div>
        ) : (
          <p className="mt-4 text-muted">
            {Object.keys(meta).length > 0
              ? "Готового снимка нет: граф изменился после прошлого анализа."
              : "Анализ ещё не запускался."}
          </p>
        ))}
    </Block>
  );
}

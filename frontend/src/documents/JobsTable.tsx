import type { Job } from "../api/types";
import { ago } from "../lib/format";
import { INGEST_STEPS, JOB_KIND_LABELS, JOB_STAGE_LABELS, labelOf } from "../lib/labels";
import { ConfirmButton } from "../ui/ConfirmButton";
import { RouterAnchor } from "../ui/RouterAnchor";
import { StatusBadge } from "../ui/StatusBadge";
import { GHOST_BUTTON, NAME_LINK, TABLE, TD, TH } from "../ui/styles";

/** Номер шага загрузки (с 1) по ключу этапа; `wiki …` — последний шаг; неизвестный — null. */
function ingestStep(stage: string): number | null {
  const key = stage.startsWith("wiki") ? "wiki" : stage;
  const index = INGEST_STEPS.indexOf(key);
  return index < 0 ? null : index + 1;
}

/** Полоска шага: с известным `total` — доля (`total` 0 — заполнена), иначе — неопределённая. */
function Bar({ done, total }: { done: number | null; total: number | null }) {
  const known = total !== null && done !== null;
  const pct = known ? (total === 0 ? 100 : Math.min(100, Math.round((done / total) * 100))) : 35;
  return (
    <div
      role="progressbar"
      aria-label="Ход шага"
      aria-valuemin={known ? 0 : undefined}
      aria-valuemax={known ? total : undefined}
      aria-valuenow={known ? done : undefined}
      className="h-1.5 w-full overflow-hidden rounded-full bg-line"
    >
      <div
        className={`h-full rounded-full bg-primary ${known ? "transition-[width] duration-500" : "motion-safe:animate-pulse"}`}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

/** Ход идущей задачи: подпись этапа и «шаг k из N» (у загрузки), полоска, счётчик. Без
 *  `stage` (старый worker) — текст `progress` как есть. */
function Running({ job }: { job: Job }) {
  if (!job.stage) return <span className="[overflow-wrap:anywhere]">{job.progress}</span>;
  const step = job.kind === "ingest" ? ingestStep(job.stage) : null;
  const counted = job.step_total !== null && job.step_done !== null;
  return (
    <div className="min-w-40 space-y-1">
      <div className="flex items-baseline justify-between gap-2">
        <span className="[overflow-wrap:anywhere]">{labelOf(JOB_STAGE_LABELS, job.stage)}</span>
        {step !== null && (
          <span className="shrink-0 text-xs text-muted">
            шаг {step} из {INGEST_STEPS.length}
          </span>
        )}
      </div>
      <div className="flex items-center gap-2">
        <Bar done={job.step_done} total={job.step_total} />
        {counted && (
          <span className="shrink-0 text-xs text-muted tabular-nums">
            {job.step_done}/{job.step_total}
          </span>
        )}
      </div>
    </div>
  );
}

/** Ход задачи: у упавшей — ошибка, у идущей — этап и полоска, иначе — когда менялась. */
function Progress({ job }: { job: Job }) {
  if (job.status === "failed") return <span className="text-danger [overflow-wrap:anywhere]">{job.error}</span>;
  if (job.status === "running") return <Running job={job} />;
  return <span className="text-muted">{ago(job.updated_at)}</span>;
}

/** Последние задачи очереди обработки; у упавших — «Повторить», у ожидающих — «Отменить». */
export function JobsTable({
  jobs,
  onRetry,
  onCancel,
  busyId = null,
}: {
  jobs: Job[];
  onRetry: (id: number) => void;
  onCancel: (id: number) => void;
  /** Действие над этой задачей уже отправлено. */
  busyId?: number | null;
}) {
  if (!jobs.length) return <p className="text-muted">Очередь пуста.</p>;
  return (
    <div className="-mx-1 overflow-x-auto px-1">
      <table className={TABLE}>
        <thead>
          <tr>
            <th className={TH}>Файл</th>
            <th className={TH}>Что</th>
            <th className={TH}>Статус</th>
            <th className={TH}>Ход</th>
            <th className={TH}>
              <span className="sr-only">Действия</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => (
            <tr key={job.id}>
              <td className={TD}>
                {job.doc_id ? (
                  <RouterAnchor href={`/documents/${encodeURIComponent(job.doc_id)}`} className={NAME_LINK}>
                    {job.name}
                  </RouterAnchor>
                ) : (
                  <span className="[overflow-wrap:anywhere]">{job.name}</span>
                )}
              </td>
              <td className={TD}>{labelOf(JOB_KIND_LABELS, job.kind)}</td>
              <td className={TD}>
                <StatusBadge value={job.status} />
              </td>
              <td className={TD}>
                <Progress job={job} />
              </td>
              <td className={`${TD} pr-0 text-right`}>
                {job.status === "failed" && (
                  <button
                    type="button"
                    disabled={busyId === job.id}
                    onClick={() => onRetry(job.id)}
                    className={GHOST_BUTTON}
                  >
                    Повторить
                  </button>
                )}
                {job.status === "pending" && (
                  <ConfirmButton
                    label="Отменить"
                    confirmLabel="Отменить задачу?"
                    busy={busyId === job.id}
                    onConfirm={() => onCancel(job.id)}
                    className="justify-end"
                  />
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

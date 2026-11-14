import type { Job } from "../api/types";
import { ago } from "../lib/format";
import { JOB_KIND_LABELS, labelOf } from "../lib/labels";
import { RouterAnchor } from "../ui/RouterAnchor";
import { StatusBadge } from "../ui/StatusBadge";
import { GHOST_BUTTON, NAME_LINK, TABLE, TD, TH } from "../ui/styles";

/** Ход задачи: у упавшей — ошибка, у идущей — `progress`, иначе — когда менялась. */
function Progress({ job }: { job: Job }) {
  if (job.status === "failed") return <span className="text-danger [overflow-wrap:anywhere]">{job.error}</span>;
  if (job.status === "running") return <span className="[overflow-wrap:anywhere]">{job.progress}</span>;
  return <span className="text-muted">{ago(job.updated_at)}</span>;
}

/** Последние задачи очереди обработки; у упавших — «Повторить». */
export function JobsTable({
  jobs,
  onRetry,
  busyId = null,
}: {
  jobs: Job[];
  onRetry: (id: number) => void;
  /** Повтор этой задачи уже отправлен. */
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
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

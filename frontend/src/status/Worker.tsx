import type { StatusPayload } from "../api/types";
import { ago, formatInt } from "../lib/format";
import { labelOf, STATUS_LABELS } from "../lib/labels";
import { Block, Dot } from "./parts";

function workerLine(worker: StatusPayload["worker"]): string {
  if (worker.alive) return `Worker работает, отмечался ${ago(worker.last_seen)}`;
  if (worker.last_seen !== null) return `Worker не отвечает: последний раз ${ago(worker.last_seen)}`;
  return "Worker ещё ни разу не запускался";
}

/** «Обработка документов»: worker, счётчики очереди, выполняемые задачи. */
export function Worker({ status }: { status: StatusPayload }) {
  const queue = Object.entries(status.queue);
  return (
    <Block title="Обработка документов">
      <p className="flex items-center gap-2 text-sm font-medium">
        <Dot tone={status.worker.alive ? "ok" : "fail"} />
        {workerLine(status.worker)}
      </p>
      <p className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-sm">
        {queue.length === 0 ? (
          <span className="text-muted">Очередь пуста.</span>
        ) : (
          queue.map(([s, n]) => (
            <span key={s}>
              {labelOf(STATUS_LABELS, s)}: <strong>{formatInt(n)}</strong>
            </span>
          ))
        )}
      </p>
      {status.running.length > 0 && (
        <ul className="mt-3 space-y-1 text-sm">
          {status.running.map((j) => (
            <li key={j.id} className="[overflow-wrap:anywhere]">{`${j.name} — ${j.progress || "начинается"}`}</li>
          ))}
        </ul>
      )}
    </Block>
  );
}

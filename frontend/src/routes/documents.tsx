import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  cancelJob,
  clearJobs,
  deleteDocument,
  DOCUMENTS_PAGE,
  documentsKey,
  hasActiveJobs,
  hasFinishedJobs,
  refreshDocuments,
  retryJob,
  useDocuments,
  useJobs,
} from "../api/documents";
import type { DocumentListItem } from "../api/types";
import { JobsTable } from "../documents/JobsTable";
import { messageOf } from "../lib/errors";
import { GHOST_BUTTON, NAME_LINK, TABLE, TD, TH } from "../ui/styles";
import { Uploader } from "../documents/Uploader";
import { ConfirmButton } from "../ui/ConfirmButton";
import { FormNote } from "../ui/form";
import { RouterAnchor } from "../ui/RouterAnchor";
import { StatusBadge } from "../ui/StatusBadge";

function Block({
  title,
  aside,
  action,
  children,
}: {
  title: string;
  /** Пометка в заголовке («обновляется»). */
  aside?: ReactNode;
  /** Кнопка справа от заголовка, вне его текста. */
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="min-w-0 rounded-[14px] bg-card p-4 shadow-card sm:p-6">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 className="flex items-baseline gap-2 text-xl font-semibold tracking-tight">
          {title}
          {aside}
        </h2>
        {action}
      </div>
      {children}
    </section>
  );
}

function DocumentsTable({
  docs,
  busyId,
  onDelete,
}: {
  docs: DocumentListItem[];
  busyId: string | null;
  onDelete: (doc: DocumentListItem) => void;
}) {
  return (
    <div className="-mx-1 overflow-x-auto px-1">
      <table className={TABLE}>
        <thead>
          <tr>
            <th className={TH}>Название</th>
            <th className={TH}>Статус</th>
            <th className={TH}>
              <span className="sr-only">Действия</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {docs.map((doc) => (
            <tr key={doc.id + doc.name}>
              <td className={TD}>
                <RouterAnchor href={`/documents/${encodeURIComponent(doc.id)}`} className={NAME_LINK}>
                  {doc.name}
                </RouterAnchor>
              </td>
              <td className={TD}>
                <StatusBadge value={doc.status} />
              </td>
              <td className={`${TD} pr-0 text-right`}>
                <ConfirmButton
                  label="Удалить"
                  confirmLabel="Удалить из базы?"
                  busy={busyId === doc.id}
                  onConfirm={() => onDelete(doc)}
                  className="justify-end"
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Документы: загрузка, список базы и очередь обработки (опрос, пока в ней есть работа). */
export function DocumentsPage() {
  const client = useQueryClient();
  const [count, setCount] = useState(DOCUMENTS_PAGE);
  const jobs = useJobs();
  const active = hasActiveJobs(jobs.data);
  const docs = useDocuments(count, { poll: active });
  const [actionError, setActionError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [jobBusy, setJobBusy] = useState<number | null>(null);
  const [clearing, setClearing] = useState(false);

  const reload = useCallback(() => void refreshDocuments(client), [client]);

  // Очередь опустела: опрос документов остановится, а их последний опрос мог пройти раньше, чем
  // worker закончил (таймеры списков независимы) — перечитываем документы ещё раз.
  const wasActive = useRef(active);
  useEffect(() => {
    if (wasActive.current && !active) void client.invalidateQueries({ queryKey: documentsKey });
    wasActive.current = active;
  }, [active, client]);

  async function remove(doc: DocumentListItem) {
    setDeleting(doc.id);
    setActionError(null);
    try {
      await deleteDocument(doc.id);
      reload();
    } catch (error) {
      setActionError(messageOf(error));
    } finally {
      setDeleting(null);
    }
  }

  /** Действие над задачей очереди («Повторить», «Отменить»): ошибка — над списками. */
  async function onJob(id: number, action: (id: number) => Promise<void>) {
    setJobBusy(id);
    setActionError(null);
    try {
      await action(id);
      reload();
    } catch (error) {
      setActionError(messageOf(error));
    } finally {
      setJobBusy(null);
    }
  }

  async function clearHistory() {
    setClearing(true);
    setActionError(null);
    try {
      await clearJobs();
      reload();
    } catch (error) {
      setActionError(messageOf(error));
    } finally {
      setClearing(false);
    }
  }

  const loadError = docs.error ?? jobs.error;

  return (
    <div className="mx-auto w-full max-w-6xl space-y-6 pt-2 pb-10 md:pt-6">
      <h1 className="text-[2.25rem] leading-tight font-semibold tracking-tight">Документы</h1>
      <Uploader onUploaded={reload} />
      {(actionError ?? loadError) && <FormNote tone="error">{actionError ?? messageOf(loadError)}</FormNote>}
      <div className="grid gap-6 lg:grid-cols-2 lg:items-start">
        <Block title="В базе">
          {docs.data === undefined ? (
            docs.isPending && <p className="text-muted">Загружаю…</p>
          ) : docs.data.items.length === 0 ? (
            <p className="text-muted">
              Здесь пока пусто. Загрузите первый документ — после обработки по нему можно будет задавать вопросы.
            </p>
          ) : (
            <>
              <DocumentsTable docs={docs.data.items} busyId={deleting} onDelete={(doc) => void remove(doc)} />
              {docs.data.more && (
                <button
                  type="button"
                  disabled={docs.isPlaceholderData}
                  onClick={() => setCount((n) => n + DOCUMENTS_PAGE)}
                  className={`mt-4 ${GHOST_BUTTON}`}
                >
                  Показать ещё
                </button>
              )}
            </>
          )}
        </Block>
        <Block
          title="Очередь"
          aside={active && <span className="text-sm font-normal text-muted">обновляется</span>}
          action={
            hasFinishedJobs(jobs.data) && (
              <ConfirmButton
                label="Очистить историю"
                confirmLabel="Скрыть завершённые?"
                busy={clearing}
                onConfirm={() => void clearHistory()}
              />
            )
          }
        >
          {jobs.data === undefined ? (
            jobs.isPending && <p className="text-muted">Загружаю…</p>
          ) : (
            <JobsTable
              jobs={jobs.data}
              onRetry={(id) => void onJob(id, retryJob)}
              onCancel={(id) => void onJob(id, cancelJob)}
              busyId={jobBusy}
            />
          )}
        </Block>
      </div>
    </div>
  );
}

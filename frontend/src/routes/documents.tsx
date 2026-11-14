import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useState, type ReactNode } from "react";
import { ApiError } from "../api/client";
import {
  deleteDocument,
  DOCUMENTS_PAGE,
  hasActiveJobs,
  refreshDocuments,
  retryJob,
  useDocuments,
  useJobs,
} from "../api/documents";
import type { DocumentListItem } from "../api/types";
import { GHOST_BUTTON, JobsTable, NAME_LINK, TABLE, TD, TH } from "../documents/JobsTable";
import { Uploader } from "../documents/Uploader";
import { ConfirmButton } from "../ui/ConfirmButton";
import { FormNote } from "../ui/form";
import { RouterAnchor } from "../ui/RouterAnchor";
import { StatusBadge } from "../ui/StatusBadge";

const messageOf = (error: unknown) => (error instanceof ApiError ? error.detail : "Ошибка запроса");

function Block({ title, aside, children }: { title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="min-w-0 rounded-[14px] bg-card p-4 shadow-card sm:p-6">
      <h2 className="mb-3 flex items-baseline gap-2 text-xl font-semibold tracking-tight">
        {title}
        {aside}
      </h2>
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
  const [retrying, setRetrying] = useState<number | null>(null);

  const reload = useCallback(() => void refreshDocuments(client), [client]);

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

  async function retry(id: number) {
    setRetrying(id);
    setActionError(null);
    try {
      await retryJob(id);
      reload();
    } catch (error) {
      setActionError(messageOf(error));
    } finally {
      setRetrying(null);
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
        <Block title="Очередь" aside={active && <span className="text-sm font-normal text-muted">обновляется</span>}>
          {jobs.data === undefined ? (
            jobs.isPending && <p className="text-muted">Загружаю…</p>
          ) : (
            <JobsTable jobs={jobs.data} onRetry={(id) => void retry(id)} busyId={retrying} />
          )}
        </Block>
      </div>
    </div>
  );
}

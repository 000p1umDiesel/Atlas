import { useQueryClient } from "@tanstack/react-query";
import { useParams, useRouter } from "@tanstack/react-router";
import { useState } from "react";
import { ApiError } from "../api/client";
import { deleteDocument, refreshDocuments, useDocument, type DocumentEntity } from "../api/documents";
import { useTypeCatalog } from "../api/queries";
import { GHOST_BUTTON, TABLE, TD, TH } from "../documents/JobsTable";
import { countOf, formatDayTime } from "../lib/format";
import { labelOf, STAGE_LABELS } from "../lib/labels";
import type { TypeCatalog } from "../lib/typeCatalog";
import { ConfirmButton } from "../ui/ConfirmButton";
import { FormNote } from "../ui/form";
import { RouterAnchor } from "../ui/RouterAnchor";
import { StatusBadge } from "../ui/StatusBadge";
import { usePanel } from "../shell/panel";

const CRUMB = "text-sm text-muted underline-offset-4 hover:text-ink hover:underline focus-visible:outline-2 focus-visible:outline-primary";
const CHUNK_FORMS: [string, string, string] = ["фрагмент", "фрагмента", "фрагментов"];
const ENTITY_FORMS: [string, string, string] = ["сущность", "сущности", "сущностей"];

function Block({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="min-w-0 rounded-[14px] bg-card p-4 shadow-card sm:p-6">
      <h2 className="mb-3 text-xl font-semibold tracking-tight">{title}</h2>
      {children}
    </section>
  );
}

/** Группы по типу: порядок типов — как в настройках, неизвестные в конце по алфавиту. */
function groupByType(entities: DocumentEntity[], catalog: TypeCatalog) {
  const groups = new Map<string, DocumentEntity[]>();
  for (const entity of entities) groups.set(entity.type, [...(groups.get(entity.type) ?? []), entity]);
  const rank = (type: string) => {
    const at = catalog.order.indexOf(type);
    return at < 0 ? Number.MAX_SAFE_INTEGER : at;
  };
  return [...groups].sort(([a], [b]) => rank(a) - rank(b) || a.localeCompare(b));
}

function Entities({ entities }: { entities: DocumentEntity[] }) {
  const catalog = useTypeCatalog();
  const { open } = usePanel();
  if (entities.length === 0) return <p className="text-muted">Сущностей пока нет.</p>;
  return (
    <div className="space-y-4">
      {groupByType(entities, catalog).map(([type, items]) => (
        <div key={type}>
          <h3 className="mb-1.5 flex items-center gap-2 text-sm font-semibold">
            <span className="size-2.5 shrink-0 rounded-full" style={{ backgroundColor: catalog.typeColor(type) }} aria-hidden="true" />
            {catalog.typeLabel(type)}
          </h3>
          <ul className="flex flex-wrap gap-x-4 gap-y-1">
            {items.map((entity) => (
              <li key={entity.entity_id} className="flex items-baseline gap-1.5">
                <button
                  type="button"
                  onClick={() => open({ kind: "entity", id: entity.entity_id })}
                  className="text-left text-sm underline-offset-4 [overflow-wrap:anywhere] hover:text-primary-ink hover:underline focus-visible:outline-2 focus-visible:outline-primary"
                >
                  {entity.name}
                </button>
                {entity.has_page && entity.slug && (
                  <RouterAnchor
                    href={`/wiki/${encodeURIComponent(entity.slug)}`}
                    title="Страница wiki"
                    className="text-xs text-primary-ink underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-primary"
                  >
                    wiki
                  </RouterAnchor>
                )}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

function Card({ id }: { id: string }) {
  const client = useQueryClient();
  const router = useRouter();
  const query = useDocument(id);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const doc = query.data;

  async function remove() {
    setBusy(true);
    setActionError(null);
    try {
      await deleteDocument(id);
      void refreshDocuments(client);
      await router.navigate({ to: "/documents" });
    } catch (error) {
      setActionError(error instanceof ApiError ? error.detail : "Ошибка запроса");
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto w-full max-w-4xl space-y-6 pt-2 pb-10 md:pt-6">
      <nav aria-label="Путь">
        <RouterAnchor href="/documents" className={CRUMB}>
          Документы
        </RouterAnchor>
      </nav>
      {query.isError ? (
        <div className="space-y-3">
          <FormNote tone="error">{query.error instanceof ApiError ? query.error.detail : "Ошибка запроса"}</FormNote>
          <RouterAnchor href="/documents" className={CRUMB}>
            К списку документов
          </RouterAnchor>
        </div>
      ) : !doc ? (
        <p className="text-muted">Загружаю…</p>
      ) : (
        <>
          <header className="space-y-3">
            <h1 className="text-[2rem] leading-tight font-semibold tracking-tight [overflow-wrap:anywhere]">{doc.name}</h1>
            <p className="flex flex-wrap items-center gap-3 text-sm text-muted">
              <StatusBadge value={doc.status} />
              <span>
                {countOf(doc.chunks, CHUNK_FORMS)}, {countOf(doc.entities.length, ENTITY_FORMS)}
              </span>
            </p>
            <div className="flex flex-wrap items-center gap-2">
              {doc.chunks > 0 && (
                <RouterAnchor href={`/documents/${encodeURIComponent(id)}/read`} className={GHOST_BUTTON}>
                  Читать
                </RouterAnchor>
              )}
              {doc.url && (
                <a href={doc.url} download className={GHOST_BUTTON}>
                  Скачать
                </a>
              )}
              <ConfirmButton label="Удалить" confirmLabel="Удалить из базы?" busy={busy} onConfirm={() => void remove()} />
            </div>
            {actionError && <FormNote tone="error">{actionError}</FormNote>}
          </header>
          <Block title="Обработка">
            {doc.stages.length === 0 ? (
              <p className="text-muted">Документ ждёт своей очереди: worker ещё не брал его в работу.</p>
            ) : (
              <div className="-mx-1 overflow-x-auto px-1">
                <table className={TABLE}>
                  <thead>
                    <tr>
                      <th className={TH}>Стадия</th>
                      <th className={TH}>Статус</th>
                      <th className={TH}>Запусков</th>
                      <th className={TH}>Когда</th>
                    </tr>
                  </thead>
                  <tbody>
                    {doc.stages.map((s) => (
                      <tr key={s.stage}>
                        <td className={TD}>
                          <div>{labelOf(STAGE_LABELS, s.stage)}</div>
                          {s.error && <div className="mt-0.5 text-danger [overflow-wrap:anywhere]">{s.error}</div>}
                        </td>
                        <td className={TD}>
                          <StatusBadge value={s.status} />
                        </td>
                        <td className={`${TD} tabular-nums`}>{s.attempts}</td>
                        <td className={`${TD} whitespace-nowrap text-muted`}>
                          {(s.updated_at && formatDayTime(s.updated_at)) || "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Block>
          <Block title="Сущности">
            <Entities entities={doc.entities} />
          </Block>
        </>
      )}
    </div>
  );
}

/** Карточка документа: стадии обработки, сущности, действия. Состояние привязано к id. */
export function DocumentPage() {
  const { id } = useParams({ strict: false }) as { id: string };
  return <Card key={id} id={id} />;
}

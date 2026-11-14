import { useEntityDocuments } from "../api/entities";
import { NAME_LINK } from "../documents/JobsTable";
import { countOf } from "../lib/format";
import { FormNote } from "../ui/form";
import { RouterAnchor } from "../ui/RouterAnchor";
import { errorText } from "./parts";

const MENTION_FORMS: [string, string, string] = ["упоминание", "упоминания", "упоминаний"];

/** Документы, упоминающие сущность, по числу упоминаний. */
export function DocumentsTab({ id }: { id: string }) {
  const query = useEntityDocuments(id);
  if (query.isError) return <FormNote tone="error">{errorText(query.error)}</FormNote>;
  if (!query.data) return <p className="text-muted">Загружаю…</p>;
  if (query.data.length === 0) return <p className="text-muted">Документов нет.</p>;
  return (
    <ul aria-label="Документы">
      {query.data.map((doc) => (
        <li
          key={doc.doc_id}
          className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-0.5 border-b border-line py-2.5 last:border-b-0"
        >
          <RouterAnchor href={`/documents/${encodeURIComponent(doc.doc_id)}`} className={`${NAME_LINK} min-w-0`}>
            {doc.name}
          </RouterAnchor>
          <span className="text-sm text-muted tabular-nums">{countOf(doc.mentions, MENTION_FORMS)}</span>
        </li>
      ))}
    </ul>
  );
}

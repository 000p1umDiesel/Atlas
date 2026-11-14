import { useQuery } from "@tanstack/react-query";
import { Download, FileText } from "lucide-react";
import { apiGet } from "../api/client";
import type { ChunkView } from "../api/types";
import { RouterAnchor } from "../ui/RouterAnchor";
import { PANEL_ACTION, PanelError, PanelLoading } from "./common";

/** Адрес чтения документа на месте фрагмента: номер — часть `chunk_id` после последнего двоеточия. */
export function chunkReadHref(chunk: Pick<ChunkView, "chunk_id" | "doc_id">): string {
  const tail = chunk.chunk_id.slice(chunk.chunk_id.lastIndexOf(":") + 1);
  const base = `/documents/${encodeURIComponent(chunk.doc_id)}/read`;
  return chunk.chunk_id.includes(":") && /^\d+$/.test(tail) ? `${base}?chunk=${Number(tail)}` : base;
}

/** Полный текст фрагмента документа (по клику на источник или сноску). */
export function ChunkPanel({ id }: { id: string }) {
  const query = useQuery<ChunkView>({
    queryKey: ["chunk", id],
    queryFn: () => apiGet<ChunkView>(`/api/chunks/${encodeURIComponent(id)}`),
  });
  if (query.isPending) return <PanelLoading />;
  if (query.isError)
    return <PanelError error={query.error} notFound="Источник удалён: документа больше нет в базе." />;
  const chunk = query.data;
  return (
    <div className="space-y-4">
      <div>
        <p className="font-medium break-words">
          {chunk.name}
          {chunk.page !== null ? `, стр. ${chunk.page}` : ""}
        </p>
        {chunk.headings.length > 0 && <p className="mt-1 text-sm text-muted">{chunk.headings.join(" › ")}</p>}
      </div>
      {/* Текст индекса, а не файл: показывается как есть, без разметки. */}
      <div className="font-serif text-[15px] leading-relaxed break-words whitespace-pre-wrap">{chunk.text}</div>
      <div className="flex flex-wrap gap-2">
        <RouterAnchor href={chunkReadHref(chunk)} className={PANEL_ACTION}>
          <FileText size={15} aria-hidden="true" />
          Открыть в документе
        </RouterAnchor>
        {chunk.url?.startsWith("/api/") && (
          <a href={chunk.url} download className={PANEL_ACTION}>
            <Download size={15} aria-hidden="true" />
            Скачать исходник
          </a>
        )}
      </div>
    </div>
  );
}

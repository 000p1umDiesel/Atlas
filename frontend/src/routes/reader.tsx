import { useParams, useRouterState } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { ApiError } from "../api/client";
import { READER_PAGE, useDocumentChunks, type ReaderChunk } from "../api/documents";
import { CRUMB, GHOST_BUTTON } from "../ui/styles";
import { prefersReducedMotion } from "../lib/motion";
import { useScrollMainToTop } from "../shell/mainScroll";
import { FormNote } from "../ui/form";
import { RouterAnchor } from "../ui/RouterAnchor";


function Pager({ offset, total, onMove }: { offset: number; total: number; onMove: (offset: number) => void }) {
  if (total <= READER_PAGE) return null;
  const end = Math.min(offset + READER_PAGE, total);
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 text-sm">
      <button type="button" disabled={offset === 0} onClick={() => onMove(Math.max(0, offset - READER_PAGE))} className={GHOST_BUTTON}>
        Назад
      </button>
      <span className="text-muted tabular-nums">
        фрагменты {offset + 1}–{end} из {total}
      </span>
      <button type="button" disabled={end >= total} onClick={() => onMove(offset + READER_PAGE)} className={GHOST_BUTTON}>
        Дальше
      </button>
    </div>
  );
}

function ChunkView({ chunk, hit, showHeading }: { chunk: ReaderChunk; hit: boolean; showHeading: boolean }) {
  const heading = chunk.headings.join(" › ");
  return (
    <section
      id={`chunk-${chunk.idx}`}
      className={`grid grid-cols-[3.5rem_1fr] gap-x-3 rounded-xl px-2 py-2 sm:grid-cols-[4.5rem_1fr] ${hit ? "hit bg-primary/10 ring-1 ring-primary/40" : ""}`}
    >
      <span
        title={`фрагмент ${chunk.idx + 1}`}
        className="pt-0.5 text-right text-xs text-muted tabular-nums"
      >
        {chunk.page_start != null ? `с. ${chunk.page_start}` : chunk.idx + 1}
      </span>
      <div className="min-w-0">
        {showHeading && heading && <h3 className="mb-1 text-sm font-semibold [overflow-wrap:anywhere]">{heading}</h3>}
        <p className="font-serif leading-relaxed whitespace-pre-wrap [overflow-wrap:anywhere]">{chunk.text}</p>
      </div>
    </section>
  );
}

function Reader({ id, chunk }: { id: string; chunk: number | null }) {
  const [offset, setOffset] = useState(chunk === null ? 0 : Math.floor(chunk / READER_PAGE) * READER_PAGE);
  const query = useDocumentChunks(id, offset);
  const data = query.data;
  const scrollToTop = useScrollMainToTop();

  // После каждой загрузки страницы выделенный фрагмент прокручивается в центр.
  useEffect(() => {
    if (!data || chunk === null) return;
    document.getElementById(`chunk-${chunk}`)?.scrollIntoView({
      block: "center",
      behavior: prefersReducedMotion() ? "auto" : "smooth",
    });
  }, [data, chunk]);

  const move = (next: number) => {
    setOffset(next);
    scrollToTop();
  };

  if (query.isError) {
    // Как у карточки: путь к списку и ссылка назад, а не тупик с одной ошибкой.
    return (
      <>
        <nav aria-label="Путь">
          <RouterAnchor href="/documents" className={CRUMB}>
            Документы
          </RouterAnchor>
        </nav>
        <FormNote tone="error">{query.error instanceof ApiError ? query.error.detail : "Ошибка запроса"}</FormNote>
        <RouterAnchor href={`/documents/${encodeURIComponent(id)}`} className={`${CRUMB} inline-block`}>
          К карточке документа
        </RouterAnchor>
      </>
    );
  }
  if (!data) return <p className="text-muted">Загружаю…</p>;

  const { document: doc } = data;
  return (
    <>
      <nav aria-label="Путь" className="flex flex-wrap items-center gap-1.5">
        <RouterAnchor href="/documents" className={CRUMB}>
          Документы
        </RouterAnchor>
        <span className="text-muted" aria-hidden="true">›</span>
        <RouterAnchor href={`/documents/${encodeURIComponent(id)}`} className={CRUMB}>
          {doc.name}
        </RouterAnchor>
      </nav>
      <h1 className="text-[2rem] leading-tight font-semibold tracking-tight [overflow-wrap:anywhere]">{doc.title || doc.name}</h1>
      <Pager offset={offset} total={data.total} onMove={move} />
      <div className="space-y-1">
        {data.chunks.map((c, i) => (
          <ChunkView
            key={c.chunk_id}
            chunk={c}
            hit={c.idx === chunk}
            showHeading={i === 0 || c.headings.join(" › ") !== data.chunks[i - 1]!.headings.join(" › ")}
          />
        ))}
      </div>
      <Pager offset={offset} total={data.total} onMove={move} />
    </>
  );
}

/** Чтение документа по фрагментам; `?chunk=<idx>` подсвечивает фрагмент и открывает его страницу. */
export function ReaderPage() {
  const { id } = useParams({ strict: false }) as { id: string };
  const raw = useRouterState({ select: (s) => (s.location.search as Record<string, unknown>).chunk });
  const parsed = typeof raw === "string" && /^\d+$/.test(raw) ? Number(raw) : null;
  // Новый документ или фрагмент — страница читается заново (оболочка страницы не пересоздаётся).
  return (
    <div className="mx-auto w-full max-w-3xl space-y-4 pt-2 pb-10 md:pt-6">
      <Reader key={`${id}|${parsed}`} id={id} chunk={parsed} />
    </div>
  );
}

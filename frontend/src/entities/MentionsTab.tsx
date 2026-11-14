import { Fragment, useId, useMemo, type MouseEvent } from "react";
import { useEntityMentions } from "../api/entities";
import type { EntityMention } from "../api/types";
import { NAME_LINK } from "../ui/styles";
import { chunkReadHref } from "../panels/ChunkPanel";
import { usePanel } from "../shell/panel";
import { FormNote } from "../ui/form";
import { RouterAnchor } from "../ui/RouterAnchor";
import { splitMatches } from "./highlight";
import { errorText, LoadMore } from "./parts";

function MentionCard({ mention: m, needles }: { mention: EntityMention; needles: string[] }) {
  const { open } = usePanel();
  const parts = useMemo(() => splitMatches(m.text, needles), [m.text, needles]);
  const textId = useId();
  const show = () => open({ kind: "chunk", id: m.chunk_id });
  // Клик по свободному месту карточки открывает фрагмент; ссылки и кнопки — сами по себе.
  const onCard = (event: MouseEvent<HTMLLIElement>) => {
    if ((event.target as HTMLElement).closest("a, button")) return;
    show();
  };
  return (
    <li
      onClick={onCard}
      className="cursor-pointer rounded-xl border border-line p-3 transition-colors hover:bg-ink/[0.03] sm:p-4"
    >
      <div className="flex flex-wrap items-baseline gap-x-2 text-sm">
        <RouterAnchor href={chunkReadHref(m)} className={NAME_LINK}>
          {m.name}
        </RouterAnchor>
        {m.page !== null && <span className="text-muted">с. {m.page}</span>}
      </div>
      {m.headings.length > 0 && (
        <p className="mt-0.5 text-xs break-words text-muted">{m.headings.join(" › ")}</p>
      )}
      {/* Текст индекса как есть: подсветка — <mark> из разбиения строки, не HTML.
          Имя кнопки короткое, а сам фрагмент — её описание: читалка не зачитывает весь текст как имя. */}
      <button
        type="button"
        onClick={show}
        aria-label={`Открыть фрагмент: ${m.name}`}
        aria-describedby={textId}
        className="mt-2 block w-full rounded-md text-left focus-visible:outline-2 focus-visible:outline-primary"
      >
        <span id={textId} className="line-clamp-6 font-serif text-[15px] leading-relaxed break-words whitespace-pre-wrap">
          {parts.map((p, i) =>
            p.match ? (
              <mark key={i} className="rounded-sm bg-primary/20 px-0.5 text-ink">
                {p.text}
              </mark>
            ) : (
              <Fragment key={i}>{p.text}</Fragment>
            ),
          )}
        </span>
      </button>
    </li>
  );
}

/** Фрагменты, упоминающие сущность, страницами по 20; имя и алиасы подсвечены. */
export function MentionsTab({ id, needles }: { id: string; needles: string[] }) {
  const query = useEntityMentions(id);
  const pages = query.data?.pages;
  const items = useMemo(() => pages?.flatMap((p) => p.items) ?? [], [pages]);
  const total = pages?.[0]?.total ?? 0;
  const canLoadMore = query.hasNextPage && !query.isFetchingNextPage;

  if (query.isError && !pages) return <FormNote tone="error">{errorText(query.error)}</FormNote>;
  if (!pages) return <p className="text-muted">Загружаю…</p>;
  if (items.length === 0) return <p className="text-muted">Упоминаний нет.</p>;
  return (
    <div aria-busy={query.isFetchingNextPage}>
      <ul aria-label="Упоминания" className="space-y-3">
        {items.map((m) => (
          <MentionCard key={m.chunk_id} mention={m} needles={needles} />
        ))}
      </ul>
      {query.isError && <FormNote tone="error">{errorText(query.error)}</FormNote>}
      {query.hasNextPage && (
        <LoadMore shown={items.length} total={total} disabled={!canLoadMore} onClick={() => void query.fetchNextPage()} />
      )}
    </div>
  );
}

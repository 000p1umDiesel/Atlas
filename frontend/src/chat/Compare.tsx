import { useEffect, useRef, useState } from "react";
import { apiSend, ApiError } from "../api/client";
import type { Source } from "../api/types";
import { formatInt, seconds } from "../lib/format";
import { MODES, type Mode } from "../lib/labels";
import { SourceList, useOpenSource } from "../sources/SourceList";
import { Markdown, type MarkdownRef } from "../ui/Markdown";

/** Ответ `/api/ask` (нужные поля). */
type Answer = {
  text: string;
  sources: Source[];
  latency_ms: number;
  tokens_in: number;
  tokens_out: number;
};

type Card =
  | { state: "queued" }
  | { state: "running" }
  | { state: "error"; detail: string }
  | { state: "done"; answer: Answer };

const QUEUED: Record<Mode, Card> = {
  rag: { state: "queued" },
  wiki: { state: "queued" },
  graph: { state: "queued" },
  all: { state: "queued" },
};

function prefersReducedMotion(): boolean {
  return (
    typeof matchMedia === "function" &&
    matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

function ModeCard({
  mode,
  card,
  onCite,
}: {
  mode: (typeof MODES)[number];
  card: Card;
  onCite: (source: Source) => void;
}) {
  let body;
  if (card.state === "queued")
    body = <p className="text-sm text-muted">В очереди</p>;
  else if (card.state === "running")
    body = (
      <p className="text-sm text-muted motion-safe:animate-pulse">Отвечаю…</p>
    );
  else if (card.state === "error") {
    body = (
      <p className="rounded-xl bg-danger/10 px-3 py-2 text-sm text-danger break-words">
        {card.detail}
      </p>
    );
  } else {
    const { answer } = card;
    const onRef = (ref: MarkdownRef) => {
      if (ref.kind !== "cite" || !Number.isInteger(ref.n)) return;
      const source = answer.sources.find((s) => s.n === ref.n);
      if (source) onCite(source);
    };
    body = (
      <>
        <p className="mb-2 text-xs text-muted tabular-nums">
          {seconds(answer.latency_ms)} · {formatInt(answer.tokens_in)} +{" "}
          {formatInt(answer.tokens_out)} токенов
        </p>
        <Markdown text={answer.text} env={{ citations: true }} onRef={onRef} />
        <SourceList sources={answer.sources} />
      </>
    );
  }
  return (
    <section
      className="min-w-0 rounded-[14px] bg-card px-4 py-3.5 shadow-card"
      title={mode.hint}
    >
      <h3 className="mb-2 text-sm font-semibold">{mode.label}</h3>
      {body}
    </section>
  );
}

/**
 * Сравнение режимов: один вопрос по очереди во всех режимах, карточка на режим. Ничего не
 * сохраняется. Смена вопроса (родитель меняет `key`) и размонтирование прерывают запросы.
 */
export function Compare({
  question,
  onClose,
}: {
  question: string;
  onClose: () => void;
}) {
  const [cards, setCards] = useState<Record<Mode, Card>>(QUEUED);
  const openSource = useOpenSource();
  const root = useRef<HTMLElement>(null);

  useEffect(() => {
    if (typeof root.current?.scrollIntoView === "function") {
      root.current.scrollIntoView({
        behavior: prefersReducedMotion() ? "auto" : "smooth",
        block: "nearest",
      });
    }
    const controller = new AbortController();
    const set = (mode: Mode, card: Card) => {
      if (!controller.signal.aborted)
        setCards((current) => ({ ...current, [mode]: card }));
    };
    setCards(QUEUED);
    void (async () => {
      for (const { value: mode } of MODES) {
        if (controller.signal.aborted) return;
        set(mode, { state: "running" });
        try {
          const answer = await apiSend<Answer>(
            "POST",
            "/api/ask",
            { question, mode },
            { signal: controller.signal },
          );
          set(
            mode,
            answer
              ? { state: "done", answer }
              : { state: "error", detail: "Пустой ответ сервера" },
          );
        } catch (error) {
          if (controller.signal.aborted) return;
          set(mode, {
            state: "error",
            detail:
              error instanceof ApiError
                ? error.detail
                : "Не удалось получить ответ",
          });
        }
      }
    })();
    return () => controller.abort();
  }, [question]);

  return (
    <section ref={root} aria-label="Сравнение режимов" className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <h2 className="font-serif text-lg font-semibold">Сравнение режимов</h2>
        <button
          type="button"
          onClick={onClose}
          className="rounded-lg px-2.5 py-1 text-sm text-muted transition-colors hover:text-ink focus-visible:outline-2 focus-visible:outline-primary"
        >
          Скрыть
        </button>
      </div>
      <p className="text-sm text-muted break-words whitespace-pre-wrap">
        {question}
      </p>
      <div className="grid gap-3 lg:grid-cols-2">
        {MODES.map((mode) => (
          <ModeCard
            key={mode.value}
            mode={mode}
            card={cards[mode.value]}
            onCite={openSource}
          />
        ))}
      </div>
    </section>
  );
}

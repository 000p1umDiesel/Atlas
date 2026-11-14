import { Check, Copy, Info, RotateCcw, Shuffle } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import type { ChatMessage } from "../api/chat";
import type { Source } from "../api/types";
import { MODES, type Mode } from "../lib/labels";
import { sourceName } from "../sources/SourceList";

/** Что можно сделать с ответом; без `question` (вопрос не найден) — только копирование. */
export type AnswerActionsProps = {
  /** Вопрос, на который это ответ (ближайшее сообщение пользователя выше). */
  question: string | null;
  /** Идёт другой ответ: отправить новый вопрос сейчас нельзя. */
  busy: boolean;
  onAsk(question: string, mode: Mode): void;
  onCompare(question: string): void;
};

const COPIED_MS = 2000;
const ACTION =
  "inline-flex items-center gap-1.5 rounded-lg px-2 py-1 text-xs text-muted transition-colors hover:bg-ink/[0.06] hover:text-ink focus-visible:outline-2 focus-visible:outline-primary disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-transparent disabled:hover:text-muted";
const CHIP =
  "rounded-full border border-line px-2.5 py-0.5 text-xs transition-colors hover:bg-ink/[0.06] focus-visible:outline-2 focus-visible:outline-primary disabled:cursor-not-allowed disabled:opacity-50";

/** Строка источника для копии: «[2] report.pdf, стр. 4». */
function sourceLine(s: Source): string {
  return `[${s.n}] ${sourceName(s)}${s.page !== null && s.page !== undefined ? `, стр. ${s.page}` : ""}`;
}

/** Текст ответа для буфера: Markdown и — если ответ на что-то сослался — список этих источников. */
export function copyText(message: ChatMessage): string {
  const cited = message.sources.filter((s) => s.cited);
  if (cited.length === 0) return message.content;
  return `${message.content.trimEnd()}\n\nИсточники:\n${cited.map(sourceLine).join("\n")}\n`;
}

/**
 * Кнопки под ответом: копировать, спросить снова, спросить в другом режиме или сравнить все
 * режимы, раскрыть «Почему такой ответ». У ответа-ошибки — только «Повторить».
 * Повтор дописывает в диалог новую пару вопрос — ответ, прежний ответ остаётся.
 */
export function AnswerActions({
  message,
  actions,
  whyOpen,
  whyId,
  onToggleWhy,
}: {
  message: ChatMessage;
  actions: AnswerActionsProps;
  whyOpen: boolean;
  whyId: string;
  onToggleWhy(): void;
}) {
  const { question, busy, onAsk, onCompare } = actions;
  const [copied, setCopied] = useState<"ok" | "failed" | null>(null);
  const [modesOpen, setModesOpen] = useState(false);
  const modesId = useId();
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);

  const failed = message.status === "error";
  const canAsk = question !== null && question.trim() !== "" && !busy;
  const mode = message.mode as Mode;

  async function copy() {
    clearTimeout(timer.current);
    try {
      if (!navigator.clipboard) throw new Error("clipboard");
      await navigator.clipboard.writeText(copyText(message));
      setCopied("ok");
    } catch {
      setCopied("failed");
    }
    timer.current = setTimeout(() => setCopied(null), COPIED_MS);
  }

  const again = (
    <button type="button" className={ACTION} disabled={!canAsk} onClick={() => question && onAsk(question, mode)}>
      <RotateCcw size={13} aria-hidden="true" />
      Повторить
    </button>
  );
  if (failed) return <div className="mt-2 flex flex-wrap gap-1">{again}</div>;

  return (
    <div className="mt-2 space-y-2 border-t border-line pt-2">
      <div className="flex flex-wrap items-center gap-1">
        <button type="button" className={ACTION} onClick={() => void copy()}>
          {copied === "ok" ? <Check size={13} aria-hidden="true" /> : <Copy size={13} aria-hidden="true" />}
          {copied === "ok" ? "Скопировано" : copied === "failed" ? "Не удалось скопировать" : "Копировать"}
        </button>
        {again}
        <button
          type="button"
          className={ACTION}
          disabled={question === null}
          aria-expanded={modesOpen}
          aria-controls={modesId}
          onClick={() => setModesOpen((v) => !v)}
        >
          <Shuffle size={13} aria-hidden="true" />
          Другой режим
        </button>
        <button type="button" className={ACTION} aria-expanded={whyOpen} aria-controls={whyId} onClick={onToggleWhy}>
          <Info size={13} aria-hidden="true" />
          Почему такой ответ
        </button>
      </div>
      {modesOpen && question !== null && (
        <div id={modesId} role="group" aria-label="Спросить в другом режиме" className="flex flex-wrap items-center gap-1.5 px-2">
          <span className="text-xs text-muted">Спросить в режиме:</span>
          {MODES.filter((m) => m.value !== mode).map((m) => (
            <button
              key={m.value}
              type="button"
              className={CHIP}
              disabled={!canAsk}
              title={m.hint}
              onClick={() => {
                setModesOpen(false);
                onAsk(question, m.value);
              }}
            >
              {m.label}
            </button>
          ))}
          <button
            type="button"
            className={`${CHIP} border-primary/40 text-primary-ink`}
            disabled={busy}
            onClick={() => {
              setModesOpen(false);
              onCompare(question);
            }}
          >
            Сравнить все режимы
          </button>
        </div>
      )}
    </div>
  );
}

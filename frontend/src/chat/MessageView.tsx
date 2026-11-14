import { useId, useState } from "react";
import type { ChatMessage } from "../api/chat";
import type { MessageMeta, Source } from "../api/types";
import { formatInt, seconds } from "../lib/format";
import { MODES } from "../lib/labels";
import { SourceList } from "../sources/SourceList";
import { Markdown, type MarkdownRef } from "../ui/Markdown";
import { AnswerActions, type AnswerActionsProps } from "./AnswerActions";
import { WhyPanel } from "./WhyPanel";

type Props = {
  message: ChatMessage;
  /** Клик по номеру `[n]` в ответе: источник с этим номером. */
  onCite: (source: Source) => void;
  /** Кнопки под завершённым ответом (в диалоге); без них ответ только читается. */
  actions?: AnswerActionsProps;
};

/** «1,5 с · 100 + 20 = 120 токенов». */
export function metricsText(meta: MessageMeta): string {
  const total = meta.tokens_in + meta.tokens_out;
  return `${seconds(meta.latency_ms)} · ${formatInt(meta.tokens_in)} + ${formatInt(meta.tokens_out)} = ${formatInt(total)} токенов`;
}

function Question({ message }: { message: ChatMessage }) {
  // Подпись режима — только у известного режима, отличного от режима по умолчанию.
  const mode = message.mode !== "rag" ? MODES.find((m) => m.value === message.mode) : undefined;
  return (
    <div className="flex flex-col items-end gap-1">
      <p className="max-w-[88%] rounded-[14px] bg-primary/15 px-4 py-2.5 break-words whitespace-pre-wrap">
        {message.content}
      </p>
      {mode && <p className="px-1 text-xs text-muted">режим: {mode.label}</p>}
    </div>
  );
}

function Answer({ message, onCite, actions }: Props) {
  const [whyOpen, setWhyOpen] = useState(false);
  const whyId = useId();
  if (message.status === "error") {
    // Только текст ошибки (сервер сохраняет «Не удалось получить ответ. Повторите вопрос.»).
    return (
      <div>
        <p className="rounded-xl bg-danger/10 px-3 py-2.5 text-sm text-danger break-words whitespace-pre-wrap">
          {message.content}
        </p>
        {actions && (
          <AnswerActions message={message} actions={actions} whyOpen={false} whyId={whyId} onToggleWhy={() => {}} />
        )}
      </div>
    );
  }
  const streaming = message.streaming === true;
  const onRef = (ref: MarkdownRef) => {
    if (ref.kind !== "cite" || !Number.isInteger(ref.n)) return; // «#cite:abc» даёт NaN
    const source = message.sources.find((s) => s.n === ref.n);
    if (source) onCite(source);
  };
  return (
    <div aria-busy={streaming} className="rounded-[14px] bg-card px-4 py-3.5 shadow-card md:px-5">
      {streaming && !message.content ? (
        <p className="text-muted motion-safe:animate-pulse">Ищу в документах и пишу ответ…</p>
      ) : (
        <Markdown text={message.content} env={{ citations: true }} onRef={onRef} />
      )}
      {!streaming && (
        <>
          <SourceList sources={message.sources} />
          {message.meta && (
            <p className="mt-2 px-2 text-xs text-muted tabular-nums" title="Время ответа и токены LLM (вход + выход)">
              {metricsText(message.meta)}
            </p>
          )}
          {actions && (
            <AnswerActions
              message={message}
              actions={actions}
              whyOpen={whyOpen}
              whyId={whyId}
              onToggleWhy={() => setWhyOpen((v) => !v)}
            />
          )}
          {actions && whyOpen && <WhyPanel id={whyId} message={message} question={actions.question} />}
        </>
      )}
    </div>
  );
}

/** Сообщение диалога: вопрос — простым текстом, ответ — Markdown с источниками и метриками. */
export function MessageView({ message, onCite, actions }: Props) {
  return message.role === "user" ? (
    <Question message={message} />
  ) : (
    <Answer message={message} onCite={onCite} actions={actions} />
  );
}

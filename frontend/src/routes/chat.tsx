import { useParams } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";
import { LIST_LIMIT, useConversations, useMessages } from "../api/chat";
import { ChatHeader } from "../chat/ChatHeader";
import { Compare } from "../chat/Compare";
import { Composer } from "../chat/Composer";
import { MessageView } from "../chat/MessageView";
import { useSend, useUnsentQuestion } from "../chat/useSend";
import { prefersReducedMotion } from "../lib/motion";
import { useOpenSource } from "../sources/SourceList";
import { FormNote } from "../ui/form";

function EmptyChat() {
  return (
    <div className="mx-auto max-w-md py-12 text-center md:py-20">
      <h1 className="font-serif text-3xl font-semibold tracking-tight">О чём спросить?</h1>
      <p className="mt-3 text-muted">
        Ответ строится по вашим документам. Номера в тексте ведут к источникам — нажмите, чтобы
        прочитать фрагмент целиком.
      </p>
    </div>
  );
}

/** Чат `/chat` и `/chat/<id>`: шапка диалога, история, поле вопроса. */
export function ChatPage() {
  const { id } = useParams({ strict: false });
  const conversations = useConversations();
  const history = useMessages(id);
  const openSource = useOpenSource();
  const { send, busy, error } = useSend(id);
  const unsent = useUnsentQuestion(id);
  // Сравнение режимов: не сохраняется; `key` — новый вопрос перезапускает цикл.
  const [compare, setCompare] = useState<{ question: string; key: number; conversation: string | undefined } | null>(null);
  // Сравнение принадлежит диалогу: при переходе в другой оно закрывается (и не вернётся).
  useEffect(() => setCompare((c) => (c && c.conversation !== id ? null : c)), [id]);
  const scroller = useRef<HTMLDivElement>(null);
  const shown = useRef<{ id: string | undefined; count: number }>({ id: undefined, count: 0 });

  const conversation = id ? conversations.data?.find((c) => c.id === id) : undefined;
  const messages = id ? (history.data ?? []) : [];
  const loading = id !== undefined && history.isPending;
  const failed = id !== undefined && history.isError;

  // К концу при новых сообщениях: открытие диалога — сразу, новое сообщение — плавно
  // (при reduced motion — тоже сразу).
  useEffect(() => {
    const box = scroller.current;
    const previous = shown.current;
    shown.current = { id, count: messages.length };
    if (!box || messages.length === 0 || (previous.id === id && previous.count === messages.length)) return;
    const smooth = previous.id === id && previous.count > 0 && !prefersReducedMotion();
    if (smooth && typeof box.scrollTo === "function") box.scrollTo({ top: box.scrollHeight, behavior: "smooth" });
    else box.scrollTop = box.scrollHeight;
  }, [id, messages.length]);

  return (
    <div className="flex h-full flex-col">
      {conversation && (
        <div className="shrink-0 border-b border-line px-4 py-3 md:px-8">
          <div className="mx-auto max-w-3xl">
            {/* Ключ — диалог: форма переименования и ошибки не переезжают в другой диалог. */}
            <ChatHeader key={conversation.id} conversation={conversation} />
          </div>
        </div>
      )}
      <div ref={scroller} className="min-h-0 flex-1 overflow-y-auto px-4 md:px-8">
        <div className="mx-auto max-w-3xl space-y-5 py-6">
          {messages.length === LIST_LIMIT && (
            <p className="text-center text-sm text-muted">Показаны последние 200 сообщений диалога.</p>
          )}
          {loading ? (
            <p className="text-muted">Загружаю…</p>
          ) : failed ? (
            <FormNote tone="error">{history.error.message}</FormNote>
          ) : messages.length === 0 ? (
            <EmptyChat />
          ) : (
            messages.map((message) => <MessageView key={message.id} message={message} onCite={openSource} />)
          )}
          {compare && compare.conversation === id && <Compare key={`${id ?? "new"}-${compare.key}`} question={compare.question} onClose={() => setCompare(null)} />}
        </div>
      </div>
      <div className="shrink-0 px-4 pt-2 pb-3 md:px-8 md:pb-5">
        <div className="mx-auto max-w-3xl space-y-2">
          {error && <FormNote tone="error">{error.message}</FormNote>}
          {/* Черновик вопроса — свой у каждого диалога (и у нового). */}
          <Composer
            key={id ?? "new"}
            busy={busy}
            initialText={unsent ?? ""}
            onSend={(question, mode, accepted) => {
              setCompare(null);
              void send(question, mode, { onAccepted: accepted });
            }}
            onCompare={(question) => setCompare((c) => ({ question, key: (c?.key ?? 0) + 1, conversation: id }))}
          />
        </div>
      </div>
    </div>
  );
}

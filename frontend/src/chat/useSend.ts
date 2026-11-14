// Отправка вопроса в диалог с ответом потоком (SSE) — §7 инвентаря.
//
// Состояние отправки (идёт ли ответ, ошибка, непринятый вопрос) живёт вне страницы: после
// первого вопроса `/chat` сменяется на `/chat/<id>`, и страница чата монтируется заново
// посреди потока. Хранилище своё у каждого QueryClient — у приложения одно, у тестов —
// своё на каждый тест.
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useRouter } from "@tanstack/react-router";
import { useCallback, useSyncExternalStore } from "react";
import { conversationsKey, createConversation, messagesKey, type ChatMessage } from "../api/chat";
import { ApiError, isUnauthorized } from "../api/client";
import { streamSse } from "../api/sse";
import type { Message, Source } from "../api/types";
import type { Mode } from "../lib/labels";

type SendState = {
  /** Ответ идёт (один на всё приложение, как в прежнем интерфейсе). */
  busy: boolean;
  /** Диалог последней отправки: чья ошибка и чей непринятый вопрос; null — новый диалог. */
  owner: string | null;
  /** Ошибка до начала ответа или обрыв: текст над полем вопроса. */
  error: ApiError | null;
  /** Вопрос, который сервер не принял: возвращается в поле своего диалога. */
  unsent: string | null;
};

const IDLE: SendState = { busy: false, owner: null, error: null, unsent: null };

class SendStore {
  private state = IDLE;
  private readonly listeners = new Set<() => void>();
  /** Номер сессии: растёт при сбросе; отправка прежней сессии больше ничего не пишет. */
  generation = 0;

  readonly subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  readonly get = () => this.state;

  set(patch: Partial<SendState>) {
    this.state = { ...this.state, ...patch };
    for (const listener of [...this.listeners]) listener();
  }

  reset() {
    this.generation += 1;
    this.set(IDLE);
  }
}

const stores = new WeakMap<QueryClient, SendStore>();

function storeFor(client: QueryClient): SendStore {
  let store = stores.get(client);
  if (!store) {
    store = new SendStore();
    stores.set(client, store);
  }
  return store;
}

/**
 * Сброс состояния отправки при смене сессии (вход, выход) — рядом с `client.clear()`:
 * ошибка и вопрос прежней сессии не показываются в новой, а ещё идущий поток прежней
 * сессии больше не трогает ни это состояние, ни кэш.
 */
export function resetSendState(client: QueryClient): void {
  storeFor(client).reset();
}

function useSendState(): SendState {
  const store = storeFor(useQueryClient());
  return useSyncExternalStore(store.subscribe, store.get);
}

/** Номер отправки: ключи оптимистичных сообщений `asked-N` / `pending-N`. */
let sequence = 0;

export type SendOptions = {
  /** Сервер принял вопрос (поток начался): можно очистить поле. */
  onAccepted?: () => void;
};

function draft(id: string, conversationId: string, over: Partial<ChatMessage>): ChatMessage {
  return {
    id,
    conversation_id: conversationId,
    user_id: "",
    role: "user",
    content: "",
    mode: "rag",
    sources: [],
    status: "done",
    query: null,
    meta: null,
    created_at: new Date().toISOString(),
    ...over,
  };
}

/**
 * Отправка вопроса в диалог `conversationId` (нет — новый диалог). `busy` — идёт ответ,
 * `error` — ошибка последней отправки в этот диалог.
 */
export function useSend(conversationId: string | undefined) {
  const client = useQueryClient();
  const router = useRouter();
  const state = useSendState();
  const owner = conversationId ?? null;

  const send = useCallback(
    async (question: string, mode: Mode, options: SendOptions = {}): Promise<void> => {
      const store = storeFor(client);
      if (store.get().busy) return;
      store.set({ busy: true, owner: conversationId ?? null, error: null, unsent: null });
      const generation = store.generation;
      // Сессия та же (не было входа или выхода): только тогда пишем состояние и кэш.
      const live = () => store.generation === generation;
      const write = (patch: Partial<SendState>) => {
        if (live()) store.set(patch);
      };
      const fresh = conversationId === undefined;
      // Новый диалог открываем, только если пользователь всё ещё на `/chat`.
      const open = (id: string) => {
        if (!live() || router.state.location.pathname !== "/chat") return;
        void router.navigate({ to: "/chat/$id", params: { id } });
      };

      let cid = conversationId;
      let accepted = false;
      const seq = ++sequence;
      const askedId = `asked-${seq}`;
      const pendingId = `pending-${seq}`;
      // Снять «отвечаю» с ожидаемого ответа (обрыв, поток без итога): иначе пузырь «отвечает»,
      // пока не придёт перечитанная история (а при обрыве сети она может не прийти).
      const settle = () => {
        if (cid === undefined || !live()) return;
        client.setQueryData<ChatMessage[]>(messagesKey(cid), (list) =>
          list?.map((message) => (message.id === pendingId ? { ...message, streaming: false } : message)),
        );
      };
      try {
        if (cid === undefined) {
          cid = (await createConversation()).id;
          write({ owner: cid });
        }
        const key = messagesKey(cid);
        // Запрос истории в полёте перезаписал бы оптимистичные сообщения.
        await client.cancelQueries({ queryKey: key });
        if (!live()) return; // пока создавался диалог, сменилась сессия
        // История ещё не загружена: после ответа перечитаем диалог целиком.
        const partial = !fresh && client.getQueryData(key) === undefined;
        client.setQueryData<ChatMessage[]>(key, (list = []) => [
          ...list,
          draft(askedId, cid!, { content: question, mode }),
          draft(pendingId, cid!, { role: "assistant", mode, streaming: true }),
        ]);

        // Заменить ожидаемый ответ; false — его уже нет в кэше (историю перечитали).
        const update = (patch: (message: ChatMessage) => Partial<ChatMessage>): boolean => {
          let applied = false;
          if (!live()) return true; // новая сессия: кэш прежней уже очищен
          client.setQueryData<ChatMessage[]>(key, (list) =>
            list?.map((message) => {
              if (message.id !== pendingId) return message;
              applied = true;
              return { ...message, ...patch(message) };
            }),
          );
          return applied;
        };

        let finished = false;
        let missed = false;
        // События идут только в кэш своего диалога: открытый в это время другой диалог
        // их не видит, а ответ сохраняется сервером в любом случае.
        await streamSse(
          `/api/conversations/${encodeURIComponent(cid)}/messages/stream`,
          { question, mode },
          (event, data) => {
            if (event === "sources") {
              update(() => ({ sources: data as Source[] }));
            } else if (event === "delta") {
              update((message) => ({ content: message.content + (data as { text: string }).text }));
            } else if (event === "done") {
              finished = true;
              missed = !update(() => ({ ...(data as Message), streaming: false }));
            } else if (event === "error") {
              finished = true;
              // Пустой или отсутствующий detail — всё равно понятный текст, а не пустой пузырь.
              const raw = (data as { detail?: unknown } | null)?.detail;
              const detail = typeof raw === "string" && raw.trim() ? raw : "Не удалось получить ответ";
              missed = !update(() => ({ id: `failed-${seq}`, status: "error", streaming: false, content: detail }));
            }
          },
          undefined,
          () => {
            accepted = true;
            if (!live()) return;
            options.onAccepted?.();
            if (fresh) {
              open(cid!);
              void client.invalidateQueries({ queryKey: conversationsKey });
            }
          },
        );
        // Поток оборвался без итога (сеть, перезапуск сервера): ответ мог сохраниться —
        // перечитываем диалог с сервера.
        if (!finished) settle();
        if (live() && (!finished || missed || partial)) void client.invalidateQueries({ queryKey: key });
      } catch (caught) {
        const error = caught instanceof ApiError ? caught : new ApiError(0, "Ошибка запроса");
        if (cid !== undefined && live()) {
          const key = messagesKey(cid);
          if (!accepted) {
            // Вопрос не принят: убрать его и пустой ответ, вопрос вернуть в поле.
            client.setQueryData<ChatMessage[]>(key, (list) =>
              list?.filter((message) => message.id !== askedId && message.id !== pendingId),
            );
          } else {
            settle(); // обрыв или испорченный поток посреди ответа
          }
          void client.invalidateQueries({ queryKey: key });
        }
        // 401 — сессия кончилась, вход уже открыт: текст ошибки после входа не нужен.
        write({ error: isUnauthorized(error) ? null : error, unsent: accepted ? null : question });
        // 401 уже увёл на вход; иначе созданный диалог открываем — там ошибка и вопрос.
        if (fresh && cid !== undefined && !accepted && !isUnauthorized(error)) open(cid);
      } finally {
        write({ busy: false });
        if (live()) void client.invalidateQueries({ queryKey: conversationsKey });
      }
    },
    [client, router, conversationId],
  );

  return { send, busy: state.busy, error: state.owner === owner ? state.error : null };
}

/** Вопрос, который сервер не принял в этом диалоге: начальный текст поля. */
export function useUnsentQuestion(conversationId: string | undefined): string | null {
  const state = useSendState();
  return state.owner === (conversationId ?? null) ? state.unsent : null;
}

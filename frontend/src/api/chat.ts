// Диалоги и история чата: запросы Query и изменения (`/api/conversations`).
import { useQuery } from "@tanstack/react-query";
import { apiGet, apiSend } from "./client";
import type { Conversation, Message } from "./types";

/** Сколько диалогов и последних сообщений диалога читаем за раз (предел сервера). */
export const LIST_LIMIT = 200;

/** Сообщение в кэше чата: `streaming` — ответ ещё приходит потоком (только на клиенте). */
export type ChatMessage = Message & { streaming?: boolean };

export const conversationsKey = ["conversations"] as const;
export const messagesKey = (id: string) => ["messages", id] as const;

const conversationPath = (id: string) => `/api/conversations/${encodeURIComponent(id)}`;

/** Диалоги пользователя, новые сверху. */
export function useConversations() {
  return useQuery<Conversation[]>({
    queryKey: conversationsKey,
    queryFn: () => apiGet<Conversation[]>("/api/conversations", { limit: LIST_LIMIT }),
  });
}

/** Последние 200 сообщений диалога по порядку; без диалога — запроса нет. */
export function useMessages(id: string | undefined) {
  return useQuery<ChatMessage[]>({
    queryKey: messagesKey(id ?? ""),
    queryFn: () =>
      apiGet<ChatMessage[]>(`${conversationPath(id!)}/messages`, { limit: LIST_LIMIT, newest: "true" }),
    enabled: id !== undefined,
  });
}

/** Новый пустой диалог (название поставит сервер по первому вопросу). */
export async function createConversation(): Promise<Conversation> {
  return (await apiSend<Conversation>("POST", "/api/conversations", {}))!;
}

/** Сервер обрезает пробелы и длину до 200 символов. */
export async function renameConversation(id: string, title: string): Promise<void> {
  await apiSend("PATCH", conversationPath(id), { title });
}

export async function deleteConversation(id: string): Promise<void> {
  await apiSend("DELETE", conversationPath(id));
}

import { Link } from "@tanstack/react-router";
import { MessageSquarePlus } from "lucide-react";
import { useConversations } from "../api/chat";

const ITEM =
  "block truncate rounded-lg px-2.5 py-1.5 text-sm transition-colors hover:bg-ink/[0.06] focus-visible:outline-2 focus-visible:outline-primary data-[status=active]:bg-primary/15 data-[status=active]:font-medium data-[status=active]:text-primary-ink";

/** Колонка чата: «Новый диалог» и диалоги пользователя (новые сверху), открытый подсвечен. */
export function ConversationList() {
  const { data, isPending, error } = useConversations();
  return (
    <nav aria-label="Диалоги" className="space-y-3">
      <Link
        to="/chat"
        activeOptions={{ exact: true, includeSearch: false }}
        className="flex items-center gap-2 rounded-xl border border-line px-3 py-2 text-sm font-medium transition-colors hover:bg-ink/[0.06] focus-visible:outline-2 focus-visible:outline-primary"
      >
        <MessageSquarePlus size={16} strokeWidth={1.9} aria-hidden="true" />
        Новый диалог
      </Link>
      {isPending ? (
        <p className="px-2.5 text-sm text-muted">Загружаю…</p>
      ) : error ? (
        <p className="px-2.5 text-sm text-danger">{error.message}</p>
      ) : data.length === 0 ? (
        <p className="px-2.5 text-sm text-muted">Диалогов пока нет.</p>
      ) : (
        <ul className="space-y-0.5">
          {data.map((c) => (
            <li key={c.id}>
              <Link
                to="/chat/$id"
                params={{ id: c.id }}
                activeOptions={{ includeSearch: false }}
                className={ITEM}
                title={c.title || undefined}
              >
                {c.title || "Без названия"}
              </Link>
            </li>
          ))}
        </ul>
      )}
    </nav>
  );
}

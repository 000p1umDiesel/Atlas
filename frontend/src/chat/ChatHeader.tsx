import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useState, type FormEvent } from "react";
import { conversationsKey, deleteConversation, messagesKey, renameConversation } from "../api/chat";
import { ApiError } from "../api/client";
import type { Conversation } from "../api/types";
import { ConfirmButton } from "../ui/ConfirmButton";
import { FormNote } from "../ui/form";

const BUTTON =
  "inline-flex items-center justify-center rounded-xl px-3 py-2 text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary disabled:cursor-wait disabled:opacity-60";
const SECONDARY = `${BUTTON} border border-line text-ink hover:bg-ink/[0.06]`;

const errorText = (err: unknown) => (err instanceof ApiError ? err.detail : "Ошибка запроса");

/** Шапка открытого диалога: название, переименование и удаление. */
export function ChatHeader({ conversation }: { conversation: Conversation }) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const name = conversation.title || "Без названия";

  const startEditing = () => {
    setTitle(conversation.title);
    setError(null);
    setEditing(true);
  };

  async function save(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await renameConversation(conversation.id, title);
      await client.invalidateQueries({ queryKey: conversationsKey });
      setEditing(false);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await deleteConversation(conversation.id);
      await navigate({ to: "/chat" });
      client.removeQueries({ queryKey: messagesKey(conversation.id) });
      await client.invalidateQueries({ queryKey: conversationsKey });
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  }

  return (
    <header className="space-y-2">
      {editing ? (
        <form onSubmit={save} className="flex flex-wrap items-center gap-2">
          <input
            aria-label="Название диалога"
            maxLength={200}
            value={title}
            autoFocus
            onChange={(event) => setTitle(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.preventDefault();
                setEditing(false);
              }
            }}
            className="min-w-0 flex-1 basis-56 rounded-xl border border-line bg-bg px-3 py-2 text-base text-ink focus-visible:border-primary focus-visible:outline-2 focus-visible:outline-primary md:text-sm"
          />
          <button
            type="submit"
            disabled={busy}
            className={`${BUTTON} bg-primary-deep text-on-primary hover:bg-primary-deep/90`}
          >
            Сохранить
          </button>
          <button type="button" onClick={() => setEditing(false)} className={`${BUTTON} text-muted hover:bg-ink/[0.06] hover:text-ink`}>
            Отмена
          </button>
        </form>
      ) : (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <h1 className="min-w-0 flex-1 basis-48 truncate text-xl font-semibold tracking-tight" title={name}>
            {name}
          </h1>
          <div className="flex flex-wrap items-center gap-2">
            <button type="button" onClick={startEditing} disabled={busy} className={SECONDARY}>
              Переименовать
            </button>
            <ConfirmButton label="Удалить" confirmLabel="Удалить диалог" onConfirm={remove} busy={busy} />
          </div>
        </div>
      )}
      {error && <FormNote tone="error">{error}</FormNote>}
    </header>
  );
}

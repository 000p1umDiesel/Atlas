import { useQueryClient } from "@tanstack/react-query";
import { useRouter, useSearch } from "@tanstack/react-router";
import { useState, type FormEvent } from "react";
import { login } from "../api/auth";
import { ApiError } from "../api/client";
import { safeNext } from "../lib/safeNext";
import { FormNote, SUBMIT_BUTTON, TextField } from "../ui/form";

function loginError(error: unknown): string {
  if (error instanceof ApiError) return error.status === 401 ? "Неверное имя или пароль" : error.detail;
  return "Ошибка запроса";
}

/** Вход: без оболочки сайта. После входа — на `next` (только путь этого сайта), иначе на главную. */
export function LoginPage() {
  const { next } = useSearch({ from: "/login" });
  const router = useRouter();
  const client = useQueryClient();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await login(username, password);
    } catch (err) {
      setError(loginError(err));
      setBusy(false);
      return;
    }
    // Данные и ошибки прежней сессии не переживают вход: всё запрашивается заново.
    client.clear();
    // Замена записи: «Назад» не возвращает на форму входа.
    await router.navigate({ href: safeNext(next), replace: true });
  }

  return (
    <main id="main" className="grid min-h-dvh place-items-center bg-bg px-4 py-10 text-ink">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex items-center justify-center gap-3">
          <span
            aria-hidden="true"
            className="grid size-10 place-items-center rounded-[14px] bg-primary-deep font-serif text-lg font-semibold text-on-primary"
          >
            m
          </span>
          <h1 className="font-serif text-3xl font-semibold tracking-tight text-primary-ink">mnogobase</h1>
        </div>
        <form onSubmit={submit} className="space-y-4 rounded-[14px] bg-card p-6 shadow-card">
          <p className="text-muted">Личная база знаний: документы, wiki и граф связей.</p>
          <TextField
            label="Имя пользователя"
            name="username"
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            required
            value={username}
            onChange={(event) => setUsername(event.target.value)}
          />
          <TextField
            label="Пароль"
            name="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
          {error && <FormNote tone="error">{error}</FormNote>}
          <button type="submit" disabled={busy} className={`${SUBMIT_BUTTON} w-full`}>
            {busy ? "Вхожу…" : "Войти"}
          </button>
        </form>
      </div>
    </main>
  );
}

import { useState, type FormEvent } from "react";
import { changePassword, useMe } from "../api/auth";
import { ApiError } from "../api/client";
import { FormNote, SUBMIT_BUTTON, TextField } from "../ui/form";

/** Не короче — как на сервере (`mnogobase user create|passwd`). */
const MIN_PASSWORD = 12;

/** Аккаунт: кто вошёл и смена пароля. */
export function AccountPage() {
  const me = useMe();
  const [current, setCurrent] = useState("");
  const [fresh, setFresh] = useState("");
  const [repeat, setRepeat] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [changed, setChanged] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setChanged(false);
    if (fresh.length < MIN_PASSWORD) return setError(`Новый пароль — не короче ${MIN_PASSWORD} символов`);
    if (fresh !== repeat) return setError("Новый пароль и повтор не совпадают");
    setBusy(true);
    setError(null);
    try {
      // 401 здесь — неверный текущий пароль, а не конец сессии: на вход не уводит.
      await changePassword(current, fresh);
      setCurrent("");
      setFresh("");
      setRepeat("");
      setChanged(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Ошибка запроса");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="mx-auto max-w-xl py-8">
      <h1 className="text-3xl font-semibold tracking-tight">Аккаунт</h1>
      {me.data && (
        <p className="mt-3 text-muted">
          Вы вошли как <strong className="font-semibold text-ink">{me.data.username}</strong>. Новых
          пользователей создают на сервере командой{" "}
          <code className="rounded-md bg-card px-1.5 py-0.5 font-mono text-sm text-ink">mnogobase user create</code>.
        </p>
      )}
      {/* noValidate: подсказка браузера о minLength опережала бы своё русское сообщение. */}
      <form onSubmit={submit} noValidate className="mt-6 space-y-4 rounded-[14px] bg-card p-6 shadow-card">
        <h2 className="text-xl font-semibold">Смена пароля</h2>
        <TextField
          label="Текущий пароль"
          type="password"
          autoComplete="current-password"
          required
          value={current}
          onChange={(event) => setCurrent(event.target.value)}
        />
        <TextField
          label="Новый пароль"
          type="password"
          autoComplete="new-password"
          required
          minLength={MIN_PASSWORD}
          value={fresh}
          onChange={(event) => setFresh(event.target.value)}
        />
        <TextField
          label="Новый пароль ещё раз"
          type="password"
          autoComplete="new-password"
          required
          value={repeat}
          onChange={(event) => setRepeat(event.target.value)}
        />
        <p className="text-sm text-muted">
          После смены пароля остальные входы (другие браузеры, CLI-клиент) завершатся.
        </p>
        {error && <FormNote tone="error">{error}</FormNote>}
        {changed && <FormNote tone="ok">Пароль изменён.</FormNote>}
        <button type="submit" disabled={busy} className={SUBMIT_BUTTON}>
          {busy ? "Меняю…" : "Сменить пароль"}
        </button>
      </form>
    </section>
  );
}

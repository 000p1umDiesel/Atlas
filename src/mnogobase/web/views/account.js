// Смена своего пароля: все прочие сессии (CLI, другие браузеры) отзываются.
import { html, useState } from "/static/vendor/htm-preact-standalone.mjs";
import { api } from "../lib/api.js";
import { ErrorNote } from "../lib/ui.js";

const MIN = 12;

export function Account({ user }) {
  const [form, setForm] = useState({ old: "", next: "", again: "" });
  const [error, setError] = useState(null);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);
  const set = (key) => (e) => setForm({ ...form, [key]: e.currentTarget.value });

  const submit = async (event) => {
    event.preventDefault();
    setDone(false);
    if (form.next.length < MIN) return setError({ detail: `Новый пароль — не короче ${MIN} символов` });
    if (form.next !== form.again) return setError({ detail: "Новый пароль и повтор не совпадают" });
    setBusy(true);
    setError(null);
    try {
      await api("/api/auth/password", {
        method: "POST",
        json: { old_password: form.old, new_password: form.next },
      });
      setForm({ old: "", next: "", again: "" });
      setDone(true);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return html`
    <section class="page-narrow">
      <h1>Аккаунт</h1>
      <p class="muted">Вы вошли как <strong>${user.username}</strong>. Новых пользователей создают на сервере командой <code>mnogobase user create</code>.</p>
      <form class="stack" onSubmit=${submit}>
        <h2>Сменить пароль</h2>
        <label>Текущий пароль
          <input type="password" autocomplete="current-password" required value=${form.old} onInput=${set("old")} />
        </label>
        <label>Новый пароль
          <input type="password" autocomplete="new-password" required minlength=${MIN} value=${form.next} onInput=${set("next")} />
        </label>
        <label>Новый пароль ещё раз
          <input type="password" autocomplete="new-password" required value=${form.again} onInput=${set("again")} />
        </label>
        <p class="muted small">После смены пароля остальные входы (другие браузеры, CLI-клиент) завершатся.</p>
        <${ErrorNote} error=${error} />
        ${done && html`<p class="note note-ok" role="status">Пароль изменён.</p>`}
        <div><button class="primary" type="submit" disabled=${busy}>${busy ? "Меняю…" : "Сменить пароль"}</button></div>
      </form>
    </section>`;
}

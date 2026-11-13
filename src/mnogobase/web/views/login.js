// Вход по имени и паролю; сессия — HttpOnly cookie, токен в JS не попадает.
import { html, useState } from "/static/vendor/htm-preact-standalone.mjs";
import { api } from "../lib/api.js";
import { ErrorNote } from "../lib/ui.js";

export function Login({ onLogin }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const user = await api("/api/auth/login", {
        method: "POST",
        json: { username, password, cookie: true },
      });
      onLogin(user);
    } catch (err) {
      setError(err.status === 401 ? { detail: "Неверное имя или пароль" } : err);
    } finally {
      setBusy(false);
    }
  };

  return html`
    <main class="login sheet">
      <form class="login-card" onSubmit=${submit}>
        <h1 class="brand-mark">mnogobase</h1>
        <p class="muted">Личная база знаний: документы, wiki и граф связей.</p>
        <label>Имя пользователя
          <input name="username" autocomplete="username" required value=${username}
            onInput=${(e) => setUsername(e.currentTarget.value)} />
        </label>
        <label>Пароль
          <input name="password" type="password" autocomplete="current-password" required
            value=${password} onInput=${(e) => setPassword(e.currentTarget.value)} />
        </label>
        <${ErrorNote} error=${error} />
        <button class="primary" type="submit" disabled=${busy}>${busy ? "Вхожу…" : "Войти"}</button>
      </form>
    </main>`;
}

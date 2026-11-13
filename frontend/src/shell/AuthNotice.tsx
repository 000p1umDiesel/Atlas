import { useQueryClient } from "@tanstack/react-query";
import { LogIn } from "lucide-react";
import { useCallback, useSyncExternalStore } from "react";
import { isUnauthorized } from "../api/client";

/**
 * Подсказка о входе, пока нет нового экрана входа (этап 3): если любой запрос получил 401,
 * предлагаем войти через старый интерфейс — его cookie `mb_session` (path `/`) действует и здесь.
 */
export function AuthNotice() {
  const client = useQueryClient();
  const cache = client.getQueryCache();
  const subscribe = useCallback((onChange: () => void) => cache.subscribe(onChange), [cache]);
  const needsLogin = useSyncExternalStore(subscribe, () =>
    cache.getAll().some((query) => isUnauthorized(query.state.error)),
  );
  if (!needsLogin) return null;
  return (
    <div
      role="status"
      className="mx-4 mt-3 flex items-start gap-3 rounded-[14px] border border-line bg-card px-4 py-3 text-sm shadow-card md:mx-8"
    >
      <LogIn size={18} strokeWidth={1.8} className="mt-0.5 shrink-0 text-primary" aria-hidden="true" />
      <p className="min-w-0">
        Нужен вход. Пока новый экран входа не готов, войдите через{" "}
        {/* Обычная навигация: /legacy — отдельная страница, не маршрут приложения. */}
        <a
          href="/legacy"
          className="font-medium text-primary-ink underline underline-offset-4 focus-visible:outline-2 focus-visible:outline-primary"
        >
          старый интерфейс
        </a>
        .
      </p>
    </div>
  );
}

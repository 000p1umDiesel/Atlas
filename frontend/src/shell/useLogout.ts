import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "@tanstack/react-router";
import { useCallback, useState } from "react";
import { logout } from "../api/auth";

/** Выход: запрос к серверу (ошибки не мешают), страница входа, пустой кэш Query. */
export function useLogout(): { logout: () => Promise<void>; busy: boolean } {
  const router = useRouter();
  const client = useQueryClient();
  const [busy, setBusy] = useState(false);
  const run = useCallback(async () => {
    setBusy(true);
    try {
      await logout();
      // Сначала уходим со страниц оболочки, потом чистим кэш: иначе смонтированные запросы
      // тут же запросили бы данные заново (уже без сессии).
      await router.navigate({ to: "/login" });
      client.clear();
    } finally {
      setBusy(false);
    }
  }, [router, client]);
  return { logout: run, busy };
}

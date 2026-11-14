// Сессия: вход ставит HttpOnly-cookie `mb_session` (сам токен скрипту не виден), выход её снимает.
import { useQuery } from "@tanstack/react-query";
import { apiGet, apiSend } from "./client";
import { fixturesMode } from "./queries";
import type { Me } from "./types";

export type { Me };

/** Текущий пользователь. Без повторов: 401 сразу уводит на вход (обработчик в App). В режиме
 * фикстур сервера нет — запрос не делается. */
export function useMe() {
  return useQuery<Me>({
    queryKey: ["me"],
    queryFn: () => apiGet<Me>("/api/auth/me"),
    retry: false,
    enabled: !fixturesMode(),
  });
}

/** Вход по паролю. 401 здесь — неверное имя или пароль, а не истёкшая сессия. */
export async function login(username: string, password: string): Promise<void> {
  await apiSend("POST", "/api/auth/login", { username, password, cookie: true }, { quiet401: true });
}

/** Выход; ошибки не мешают — пользователь всё равно уходит на страницу входа. */
export async function logout(): Promise<void> {
  try {
    await apiSend("POST", "/api/auth/logout", undefined, { quiet401: true });
  } catch {
    // сессия могла уже кончиться или сервер недоступен
  }
}

/** Смена пароля. 401 — неверный текущий пароль: показываем текст, на вход не уводим. */
export async function changePassword(oldPassword: string, newPassword: string): Promise<void> {
  await apiSend(
    "POST",
    "/api/auth/password",
    { old_password: oldPassword, new_password: newPassword },
    { quiet401: true },
  );
}

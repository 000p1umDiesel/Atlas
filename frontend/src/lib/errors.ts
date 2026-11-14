import { ApiError } from "../api/client";

/** Текст ошибки запроса для пользователя: у `ApiError` — ответ сервера, иначе общий. */
export const messageOf = (error: unknown) => (error instanceof ApiError ? error.detail : "Ошибка запроса");

// Загрузка файла с прогрессом: XHR умеет `upload.onprogress`, fetch — нет.
import { ApiError, detailOf, notifyUnauthorized } from "./client";
import type { UploadedJob } from "./types";

/** Ответ загрузки (202): задача очереди и `duplicate` — такой файл уже загружали. */
export type UploadJob = UploadedJob;

/**
 * `POST /api/documents?filename=<имя>`, тело — сам файл (Content-Type не ставим), cookie сессии.
 * `onProgress(доля 0..1)` — пока известен размер тела. Сбой сети — `ApiError(0, …)`;
 * не-2xx — `ApiError(код, detail)`, 401 будит подписчиков `onUnauthorized` (переход на вход).
 */
export function uploadFile(file: File, onProgress: (fraction: number) => void): Promise<UploadJob> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/documents?filename=${encodeURIComponent(file.name)}`);
    xhr.withCredentials = true;
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) onProgress(event.loaded / event.total);
    };
    xhr.onerror = () => reject(new ApiError(0, "Нет связи с сервером"));
    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        // не JSON (например, страница прокси) — текст по коду ответа
      }
      if (xhr.status >= 200 && xhr.status < 300 && body && typeof body === "object") {
        resolve(body as UploadJob);
        return;
      }
      if (xhr.status === 401) notifyUnauthorized();
      reject(new ApiError(xhr.status, detailOf(body, xhr.status)));
    };
    xhr.send(file);
  });
}

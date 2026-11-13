// Обращения к API mnogobase из браузера: cookie-сессия (same-origin), JSON и SSE.
// 401 в любом ответе — сессия кончилась: вызывается обработчик, заданный приложением
// (показать вход), один раз на волну ошибок, без повторов запроса.

export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === "string" ? detail : "Ошибка запроса");
    this.status = status;
    this.detail = this.message;
  }
}

let unauthorized = () => {};

export function onUnauthorized(handler) {
  unauthorized = handler;
}

function detailOf(body, status) {
  if (body && typeof body.detail === "string") return body.detail;
  if (status === 422) return "Проверьте введённые данные";
  if (status === 413) return "Слишком большой запрос";
  if (status >= 500) return "Сервис временно недоступен";
  return "Ошибка запроса";
}

async function failure(response) {
  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (response.status === 401) unauthorized();
  return new ApiError(response.status, detailOf(body, response.status));
}

export async function api(path, { method = "GET", json, body, signal } = {}) {
  const headers = {};
  if (json !== undefined) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: json !== undefined ? JSON.stringify(json) : body,
      credentials: "same-origin",
      signal,
    });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new ApiError(0, "Нет связи с сервером");
  }
  if (!response.ok) throw await failure(response);
  return response.status === 204 ? null : response.json();
}

// POST с JSON и ответом text/event-stream. onEvent(name, data) вызывается на каждое событие.
export async function streamSse(path, json, onEvent, { signal } = {}) {
  let response;
  try {
    response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(json),
      credentials: "same-origin",
      signal,
    });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new ApiError(0, "Нет связи с сервером");
  }
  if (!response.ok) throw await failure(response);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      let name = "message";
      const data = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) name = line.slice(7);
        else if (line.startsWith("data: ")) data.push(line.slice(6));
      }
      if (data.length) onEvent(name, JSON.parse(data.join("\n")));
    }
  }
}

// Загрузка файла с прогрессом: XHR умеет upload.onprogress, fetch — нет.
export function uploadFile(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/documents?filename=" + encodeURIComponent(file.name));
    xhr.withCredentials = true;
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    };
    xhr.onerror = () => reject(new ApiError(0, "Нет связи с сервером"));
    xhr.onload = () => {
      let body = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        body = null;
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
      else {
        if (xhr.status === 401) unauthorized();
        reject(new ApiError(xhr.status, detailOf(body, xhr.status)));
      }
    };
    xhr.send(file);
  });
}

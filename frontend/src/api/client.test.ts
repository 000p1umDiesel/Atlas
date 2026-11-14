import { ApiError, apiGet, apiSend, onUnauthorized } from "./client";

const fetchMock = vi.fn();

/** Ответ сервера: объект уходит как JSON, строка — как есть (например, HTML прокси), null — без тела (204). */
function respond(status: number, body: unknown, statusText = "") {
  const text = body === null ? null : typeof body === "string" ? body : JSON.stringify(body);
  const headers = { "Content-Type": typeof body === "string" ? "text/html" : "application/json" };
  fetchMock.mockResolvedValueOnce(new Response(text, { status, statusText, headers }));
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

it("serializes repeated params and skips undefined", async () => {
  respond(200, { total: 0, items: [] });
  await apiGet("/api/entities", { type: ["Person", "Work"], q: undefined, limit: 10 });
  expect(fetchMock).toHaveBeenCalledWith(
    "/api/entities?type=Person&type=Work&limit=10",
    expect.objectContaining({ credentials: "same-origin" }),
  );
});

it("sends Accept: application/json and returns the parsed body", async () => {
  respond(200, { documents: 3 });
  await expect(apiGet("/api/stats")).resolves.toEqual({ documents: 3 });
  const [url, init] = fetchMock.mock.calls[0]!;
  expect(url).toBe("/api/stats");
  expect(new Headers(init.headers).get("Accept")).toBe("application/json");
});

it("throws ApiError with detail", async () => {
  respond(404, { detail: "Сущность не найдена" });
  const error = apiGet("/api/entities/x");
  await expect(error).rejects.toBeInstanceOf(ApiError);
  await expect(error).rejects.toMatchObject({ status: 404, detail: "Сущность не найдена" });
});

it("falls back to a Russian text by status for non-JSON errors", async () => {
  respond(503, "<html>", "Service Unavailable");
  await expect(apiGet("/api/stats")).rejects.toMatchObject({ status: 503, detail: "Сервис временно недоступен" });
  respond(422, { detail: [{ msg: "field required" }] });
  await expect(apiGet("/api/stats")).rejects.toMatchObject({ status: 422, detail: "Проверьте введённые данные" });
  respond(404, "<html>", "Not Found");
  await expect(apiGet("/api/stats")).rejects.toMatchObject({ status: 404, detail: "Ошибка запроса" });
});

it("uses the error message as the visible text", async () => {
  respond(401, { detail: "Требуется вход" });
  await expect(apiGet("/api/stats")).rejects.toMatchObject({ message: "Требуется вход" });
});

it("apiSend posts JSON and returns null on 204", async () => {
  respond(204, null);
  await expect(apiSend("DELETE", "/api/conversations/c1")).resolves.toBeNull();
  respond(201, { id: "c1" });
  await apiSend("POST", "/api/conversations", {});
  expect(fetchMock).toHaveBeenLastCalledWith(
    "/api/conversations",
    expect.objectContaining({
      method: "POST",
      credentials: "same-origin",
      body: "{}",
      headers: expect.objectContaining({ "Content-Type": "application/json" }),
    }),
  );
});

it("apiSend without json sends no body and no Content-Type", async () => {
  respond(204, null);
  await apiSend("DELETE", "/api/conversations/c1");
  const [, init] = fetchMock.mock.calls[0]!;
  expect(init.body).toBeUndefined();
  expect(new Headers(init.headers).has("Content-Type")).toBe(false);
});

it("maps missing detail by status and network failure", async () => {
  respond(413, "<html>");
  await expect(apiSend("POST", "/x", {})).rejects.toMatchObject({ status: 413, detail: "Слишком большой запрос" });
  fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
  await expect(apiGet("/x")).rejects.toMatchObject({ status: 0, detail: "Нет связи с сервером" });
  fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
  await expect(apiSend("POST", "/x", {})).rejects.toMatchObject({ status: 0, detail: "Нет связи с сервером" });
});

it("rethrows AbortError as is", async () => {
  const abort = new DOMException("The operation was aborted.", "AbortError");
  fetchMock.mockRejectedValueOnce(abort);
  await expect(apiGet("/x")).rejects.toBe(abort);
});

it("notifies unauthorized handlers on 401", async () => {
  const seen = vi.fn();
  const off = onUnauthorized(seen);
  respond(401, { detail: "Нужен вход" });
  await expect(apiGet("/api/stats")).rejects.toMatchObject({ status: 401 });
  expect(seen).toHaveBeenCalledTimes(1);
  respond(401, { detail: "Нужен вход" });
  await expect(apiSend("POST", "/api/conversations", {})).rejects.toMatchObject({ status: 401 });
  expect(seen).toHaveBeenCalledTimes(2);
  off();
  respond(401, { detail: "Нужен вход" });
  await expect(apiGet("/api/stats")).rejects.toMatchObject({ status: 401 });
  expect(seen).toHaveBeenCalledTimes(2);
});

it("quiet401 keeps 401 from reaching the handlers", async () => {
  const seen = vi.fn();
  const off = onUnauthorized(seen);
  respond(401, { detail: "Неверный текущий пароль" });
  await expect(apiSend("POST", "/api/auth/password", {}, { quiet401: true })).rejects.toMatchObject({
    status: 401,
    detail: "Неверный текущий пароль",
  });
  expect(seen).not.toHaveBeenCalled();
  off();
});

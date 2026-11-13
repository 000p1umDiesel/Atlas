import { ApiError, apiGet } from "./client";

const fetchMock = vi.fn();

/** Ответ сервера: объект уходит как JSON, строка — как есть (например, HTML прокси). */
function respond(status: number, body: unknown, statusText = "") {
  const text = typeof body === "string" ? body : JSON.stringify(body);
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

it("falls back to statusText for non-JSON errors", async () => {
  respond(503, "<html>", "Service Unavailable");
  await expect(apiGet("/api/stats")).rejects.toMatchObject({ status: 503, detail: "Service Unavailable" });
});

it("uses the error message as the visible text", async () => {
  respond(401, { detail: "Требуется вход" });
  await expect(apiGet("/api/stats")).rejects.toMatchObject({ message: "Требуется вход" });
});

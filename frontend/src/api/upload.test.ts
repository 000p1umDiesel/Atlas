import { installFakeXhr } from "../test/fakeXhr";
import { ApiError, onUnauthorized } from "./client";
import type { Job } from "./types";
import { uploadFile } from "./upload";

const JOB: Job = {
  id: 7,
  kind: "ingest",
  status: "pending",
  attempts: 0,
  available_at: 1_800_000_000,
  error: null,
  progress: "",
  created_at: 1_800_000_000,
  updated_at: 1_800_000_000,
  doc_id: null,
  name: "отчёт 1.pdf",
  stage: null,
  step_done: null,
  step_total: null,
};

const file = (name = "отчёт 1.pdf") => new File(["%PDF"], name, { type: "application/pdf" });

afterEach(() => vi.unstubAllGlobals());

test("POST с закодированным именем, тело — сам файл, cookie, прогресс и ответ 202", async () => {
  const xhrs = installFakeXhr();
  const onProgress = vi.fn();
  const f = file();
  const result = uploadFile(f, onProgress);

  const xhr = xhrs[0]!;
  expect(xhr.method).toBe("POST");
  expect(xhr.url).toBe(`/api/documents?filename=${encodeURIComponent("отчёт 1.pdf")}`);
  expect(xhr.url).toContain("%D0%BE%D1%82%D1%87%D1%91%D1%82%201.pdf");
  expect(xhr.body).toBe(f);
  expect(xhr.withCredentials).toBe(true);

  xhr.progress(50, 100);
  expect(onProgress).toHaveBeenCalledWith(0.5);
  xhr.progress(10, 0, false); // длина неизвестна — без вызова
  expect(onProgress).toHaveBeenCalledTimes(1);

  xhr.respond(202, { ...JOB, duplicate: false });
  await expect(result).resolves.toEqual({ ...JOB, duplicate: false });
});

test("413 — ApiError с detail сервера", async () => {
  const xhrs = installFakeXhr();
  const result = uploadFile(file(), () => {});
  xhrs[0]!.respond(413, { detail: "Загрузка слишком большая" });
  const error = await result.catch((e: unknown) => e);
  expect(error).toBeInstanceOf(ApiError);
  expect(error).toMatchObject({ status: 413, detail: "Загрузка слишком большая" });
});

test("ответ без JSON — текст по коду ответа", async () => {
  const xhrs = installFakeXhr();
  const result = uploadFile(file(), () => {});
  xhrs[0]!.respond(502, "<html>Bad Gateway</html>");
  await expect(result).rejects.toMatchObject({ status: 502, detail: "Сервис временно недоступен" });
});

test("сетевой сбой — ApiError(0, «Нет связи с сервером»)", async () => {
  const xhrs = installFakeXhr();
  const result = uploadFile(file(), () => {});
  xhrs[0]!.fail();
  const error = await result.catch((e: unknown) => e);
  expect(error).toBeInstanceOf(ApiError);
  expect(error).toMatchObject({ status: 0, detail: "Нет связи с сервером" });
});

test.each(["abort", "timeout"] as const)("%s — тоже «Нет связи с сервером», обещание не висит", async (how) => {
  const xhrs = installFakeXhr();
  const result = uploadFile(file(), () => {});
  xhrs[0]![how]();
  await expect(result).rejects.toMatchObject({ status: 0, detail: "Нет связи с сервером" });
});

test("2xx без объекта в теле — ошибка, а не «успех» без задачи", async () => {
  const xhrs = installFakeXhr();
  const result = uploadFile(file(), () => {});
  xhrs[0]!.respond(202, "<html>proxy</html>");
  const error = await result.catch((e: unknown) => e);
  expect(error).toBeInstanceOf(ApiError);
  expect(error).toMatchObject({ status: 0, detail: "Сервис временно недоступен" });
});

test("401 — подписчики onUnauthorized уведомлены", async () => {
  const xhrs = installFakeXhr();
  const handler = vi.fn();
  const off = onUnauthorized(handler);
  try {
    const result = uploadFile(file(), () => {});
    xhrs[0]!.respond(401, { detail: "Требуется вход" });
    await expect(result).rejects.toMatchObject({ status: 401, detail: "Требуется вход" });
    expect(handler).toHaveBeenCalledTimes(1);
  } finally {
    off();
  }
});

test("не-401 ошибки не уведомляют подписчиков", async () => {
  const xhrs = installFakeXhr();
  const handler = vi.fn();
  const off = onUnauthorized(handler);
  try {
    const result = uploadFile(file(), () => {});
    xhrs[0]!.respond(429, { detail: "Очередь обработки заполнена" });
    await expect(result).rejects.toMatchObject({ status: 429 });
    expect(handler).not.toHaveBeenCalled();
  } finally {
    off();
  }
});

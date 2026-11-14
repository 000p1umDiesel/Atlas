import { createMemoryHistory } from "@tanstack/react-router";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import type { DocumentListItem, Job } from "../api/types";
import { App, createQueryClient } from "../App";
import { createAppRouter } from "../router";
import { fail, fakeApi, type FakeApi } from "../test/fakeApi";

const ME = { id: "u1", username: "owner" };
const NOW = Math.floor(Date.now() / 1000);

const doc = (id: string, name: string, status = "done"): DocumentListItem => ({
  id,
  name,
  status,
  uploaded: true,
  url: `/api/documents/${id}/content`,
});

const job = (over: Partial<Job>): Job => ({
  id: 1,
  kind: "ingest",
  status: "done",
  attempts: 1,
  available_at: NOW,
  error: null,
  progress: "",
  created_at: NOW - 600,
  updated_at: NOW - 300,
  doc_id: null,
  name: "a.pdf",
  stage: null,
  step_done: null,
  step_total: null,
  ...over,
});

const DOCS = "GET /api/documents?limit=200&offset=0";
const JOBS = "GET /api/jobs?limit=30";

type Routes = Parameters<typeof fakeApi>[0];

function routes(extra: Routes = {}): Routes {
  return {
    "GET /api/auth/me": ME,
    [DOCS]: [doc("d1", "Смит.pdf"), doc("d2", "Рикардо.docx", "failed")],
    [JOBS]: [],
    ...extra,
  };
}

async function renderAt(path: string, api: FakeApi) {
  vi.stubGlobal("fetch", api);
  const router = createAppRouter({ history: createMemoryHistory({ initialEntries: [path] }) });
  await router.load();
  render(<App router={router} client={createQueryClient()} />);
  return router;
}

async function click(el: HTMLElement) {
  await act(async () => {
    fireEvent.click(el);
    await new Promise((r) => setTimeout(r, 0));
  });
}

const count = (api: FakeApi, request: string) => api.requests().filter((r) => r === request).length;
const section = (name: string) => screen.getByRole("heading", { level: 2, name: new RegExp(`^${name}`) }).closest("section")!;

beforeAll(() => {
  window.scrollTo = vi.fn(); // jsdom не реализует scrollTo, роутер его зовёт
});
beforeEach(() => vi.stubEnv("VITE_FIXTURES", ""));
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

test("список документов: ссылки на карточки, статусы, удаление; очередь пуста", async () => {
  await renderAt("/documents", fakeApi(routes()));
  expect(await screen.findByRole("heading", { level: 1, name: "Документы" })).toBeTruthy();
  const docs = section("В базе");
  const link = await within(docs).findByRole("link", { name: "Смит.pdf" });
  expect(link.getAttribute("href")).toBe("/documents/d1");
  const headers = within(docs).getAllByRole("columnheader").map((th) => th.textContent);
  expect(headers.slice(0, 2)).toEqual(["Название", "Статус"]);
  expect(within(docs).getByText("готово")).toBeTruthy();
  expect(within(docs).getByText("ошибка")).toBeTruthy();
  expect(within(docs).getAllByRole("button", { name: "Удалить" })).toHaveLength(2);
  expect(within(docs).queryByRole("button", { name: "Показать ещё" })).toBeNull();
  expect(within(section("Очередь")).getByText("Очередь пуста.")).toBeTruthy();
  expect(within(section("Очередь")).queryByText("обновляется")).toBeNull();
});

test("пустая база — пояснение", async () => {
  await renderAt("/documents", fakeApi(routes({ [DOCS]: [] })));
  expect(
    await screen.findByText(
      "Здесь пока пусто. Загрузите первый документ — после обработки по нему можно будет задавать вопросы.",
    ),
  ).toBeTruthy();
});

test("удаление в два клика — DELETE и перезапрос списка", async () => {
  const api = fakeApi(routes({ "DELETE /api/documents/d1": job({ id: 9, kind: "remove", status: "pending" }) }));
  await renderAt("/documents", api);
  const docs = section("В базе");
  await within(docs).findByRole("link", { name: "Смит.pdf" });
  const row = within(docs).getByRole("link", { name: "Смит.pdf" }).closest("tr")!;
  await click(within(row).getByRole("button", { name: "Удалить" }));
  expect(api.requests()).not.toContain("DELETE /api/documents/d1");
  await click(within(row).getByRole("button", { name: "Удалить из базы?" }));
  expect(api.requests()).toContain("DELETE /api/documents/d1");
  await vi.waitFor(() => expect(count(api, DOCS)).toBe(2));
  expect(count(api, JOBS)).toBe(2);
});

test("ошибка удаления — текст detail", async () => {
  const api = fakeApi(
    routes({ "DELETE /api/documents/d1": () => Response.json({ detail: "Очередь обработки заполнена" }, { status: 429 }) }),
  );
  await renderAt("/documents", api);
  const row = (await screen.findByRole("link", { name: "Смит.pdf" })).closest("tr")!;
  await click(within(row).getByRole("button", { name: "Удалить" }));
  await click(within(row).getByRole("button", { name: "Удалить из базы?" }));
  expect((await screen.findByRole("alert")).textContent).toContain("Очередь обработки заполнена");
});

test("таблица очереди: подписи, ход, ссылка на документ, «Повторить» у ошибки", async () => {
  const jobs = [
    job({ id: 1, kind: "ingest", status: "done", doc_id: "d1", name: "Смит.pdf" }),
    job({ id: 2, kind: "remove", status: "failed", error: "QdrantError", name: "old.pdf" }),
    job({ id: 3, kind: "analyze", status: "superseded", name: "граф" }),
  ];
  const api = fakeApi(routes({ [JOBS]: jobs, "POST /api/jobs/2/retry": job({ id: 2, status: "pending" }) }));
  await renderAt("/documents", api);
  const queue = section("Очередь");
  await within(queue).findByText("old.pdf");
  const headers = within(queue).getAllByRole("columnheader").map((th) => th.textContent);
  expect(headers.slice(0, 4)).toEqual(["Файл", "Что", "Статус", "Ход"]);
  expect(within(queue).getByRole("link", { name: "Смит.pdf" }).getAttribute("href")).toBe("/documents/d1");
  expect(within(queue).queryByRole("link", { name: "old.pdf" })).toBeNull();
  expect(within(queue).getByText("загрузка")).toBeTruthy();
  expect(within(queue).getByText("удаление")).toBeTruthy();
  expect(within(queue).getByText("анализ графа")).toBeTruthy();
  expect(within(queue).getByText("QdrantError")).toBeTruthy();
  expect(within(queue).getByText("заменена")).toBeTruthy();
  expect(within(queue).getAllByText("5 минут назад")).toHaveLength(2);

  const retry = within(queue).getAllByRole("button", { name: "Повторить" });
  expect(retry).toHaveLength(1);
  await click(retry[0]!);
  expect(api.requests()).toContain("POST /api/jobs/2/retry");
  await vi.waitFor(() => expect(count(api, JOBS)).toBe(2));
});

test("задача в работе: ход из progress, «обновляется» и опрос раз в 3000 мс", async () => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  const api = fakeApi(routes({ [JOBS]: [job({ id: 5, status: "running", progress: "embed 3/10", name: "b.pdf" })] }));
  await renderAt("/documents", api);
  await vi.waitFor(() => expect(screen.getByText("embed 3/10")).toBeTruthy());
  expect(within(section("Очередь")).getByText("обновляется")).toBeTruthy();
  expect(count(api, JOBS)).toBe(1);
  expect(count(api, DOCS)).toBe(1);

  await act(async () => {
    vi.advanceTimersByTime(3000);
  });
  await vi.waitFor(() => expect(count(api, JOBS)).toBe(2));
  await vi.waitFor(() => expect(count(api, DOCS)).toBe(2));
});

test("без активных задач опроса нет", async () => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  const api = fakeApi(routes({ [JOBS]: [job({ id: 5, status: "done", name: "b.pdf" })] }));
  await renderAt("/documents", api);
  await vi.waitFor(() => expect(screen.getByText("b.pdf")).toBeTruthy());
  await act(async () => {
    vi.advanceTimersByTime(10_000);
  });
  expect(count(api, JOBS)).toBe(1);
  expect(count(api, DOCS)).toBe(1);
});

test("опрос прекращается, когда задачи закончились; документы перечитываются в этот момент", async () => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  let status: Job["status"] = "pending";
  let docStatus = "running";
  const api = fakeApi(
    routes({
      [DOCS]: () => Response.json([doc("d1", "Смит.pdf", docStatus)]),
      // worker закончил, пока шёл запрос очереди: документы того же такта прочитаны старыми
      [JOBS]: () => {
        if (status !== "done") return Response.json([job({ id: 5, status, name: "b.pdf" })]);
        return new Promise<Response>((resolve) =>
          setTimeout(() => {
            docStatus = "done";
            resolve(Response.json([job({ id: 5, status, name: "b.pdf" })]));
          }, 0),
        );
      },
    }),
  );
  await renderAt("/documents", api);
  await vi.waitFor(() => expect(screen.getByText("обновляется")).toBeTruthy());
  expect(within(section("В базе")).getByText("в работе")).toBeTruthy();
  status = "done";
  await act(async () => {
    vi.advanceTimersByTime(3000);
  });
  await vi.waitFor(() => expect(screen.queryByText("обновляется")).toBeNull());
  await vi.waitFor(() => expect(within(section("В базе")).getByText("готово")).toBeTruthy());
  const jobs = count(api, JOBS);
  const docs = count(api, DOCS);
  await act(async () => {
    vi.advanceTimersByTime(9000);
  });
  expect(count(api, JOBS)).toBe(jobs);
  expect(count(api, DOCS)).toBe(docs);
});

test("«Показать ещё»: страницы по 200 и проба следующей", async () => {
  const page = (from: number, n: number) => Array.from({ length: n }, (_, i) => doc(`d${from + i}`, `Документ ${from + i}`));
  const api = fakeApi(
    routes({
      [DOCS]: page(0, 200),
      "GET /api/documents?limit=1&offset=200": [doc("d200", "Документ 200")],
      "GET /api/documents?limit=200&offset=200": page(200, 3),
    }),
  );
  await renderAt("/documents", api);
  const more = await screen.findByRole("button", { name: "Показать ещё" });
  expect(screen.getByRole("link", { name: "Документ 199" })).toBeTruthy();
  await click(more);
  expect(await screen.findByRole("link", { name: "Документ 202" })).toBeTruthy();
  expect(api.requests()).toContain("GET /api/documents?limit=200&offset=200");
  expect(screen.queryByRole("button", { name: "Показать ещё" })).toBeNull();
});

test("ссылка на документ сохраняет панель на этой странице", async () => {
  const router = await renderAt("/documents?panel=entity%3Ae1", fakeApi(routes()));
  await screen.findByRole("link", { name: "Смит.pdf" });
  expect(router.state.location.search).toMatchObject({ panel: "entity:e1" });
});

test("прогресс: подпись этапа, шаг и полоска", async () => {
  const jobs = [
    job({ id: 1, status: "running", kind: "ingest", stage: "embed", step_done: 3, step_total: 12, name: "a.pdf" }),
    job({ id: 2, status: "running", kind: "ingest", stage: "parse", name: "b.pdf" }),
    job({ id: 3, status: "running", kind: "ingest", stage: "embed", step_done: 0, step_total: 0, name: "c.pdf" }),
    job({ id: 4, status: "running", kind: "analyze", stage: "analyze", name: "граф" }),
    job({ id: 5, status: "running", kind: "ingest", stage: "wiki draft", step_done: 1, step_total: 2, name: "d.pdf" }),
  ];
  await renderAt("/documents", fakeApi(routes({ [JOBS]: jobs })));
  const queue = section("Очередь");
  const rowOf = async (name: string) => (await within(queue).findByText(name)).closest("tr")!;

  const embed = await rowOf("a.pdf");
  expect(within(embed).getByText("Получение эмбеддингов")).toBeTruthy();
  expect(within(embed).getByText("шаг 3 из 6")).toBeTruthy();
  expect(within(embed).getByText("3/12")).toBeTruthy();
  const bar = within(embed).getByRole("progressbar");
  expect(bar.getAttribute("aria-valuenow")).toBe("3");
  expect(bar.getAttribute("aria-valuemax")).toBe("12");

  const parse = await rowOf("b.pdf");
  expect(within(parse).getByText("Разбор документа")).toBeTruthy();
  expect(within(parse).getByText("шаг 1 из 6")).toBeTruthy();
  expect(within(parse).getByRole("progressbar").getAttribute("aria-valuenow")).toBeNull();

  const empty = await rowOf("c.pdf");
  const fill = within(empty).getByRole("progressbar").firstElementChild as HTMLElement;
  expect(fill.style.width).toBe("100%");

  const analyze = await rowOf("граф");
  expect(within(analyze).getByText("Анализ графа")).toBeTruthy();
  expect(within(analyze).queryByText(/шаг/)).toBeNull();

  const wiki = await rowOf("d.pdf");
  expect(within(wiki).getByText("Wiki: написание страниц")).toBeTruthy();
  expect(within(wiki).getByText("шаг 6 из 6")).toBeTruthy();
});

test("«Отменить» у ожидающей задачи; у идущей и готовой кнопки нет", async () => {
  const jobs = [
    job({ id: 4, status: "pending", kind: "remove", name: "old.pdf" }),
    job({ id: 5, status: "running", stage: "parse", name: "b.pdf" }),
    job({ id: 6, status: "done", name: "c.pdf" }),
  ];
  const api = fakeApi(routes({ [JOBS]: jobs, "POST /api/jobs/4/cancel": job({ id: 4, status: "cancelled" }) }));
  await renderAt("/documents", api);
  const queue = section("Очередь");
  await within(queue).findByText("old.pdf");
  const buttons = within(queue).getAllByRole("button", { name: "Отменить" });
  expect(buttons).toHaveLength(1);
  await click(buttons[0]!);
  await click(within(queue).getByRole("button", { name: "Отменить задачу?" }));
  expect(api.requests()).toContain("POST /api/jobs/4/cancel");
  await vi.waitFor(() => expect(count(api, JOBS)).toBe(2));
});

test("отмена, которую worker опередил: текст ошибки", async () => {
  const api = fakeApi(
    routes({
      [JOBS]: [job({ id: 4, status: "pending", name: "old.pdf" })],
      "POST /api/jobs/4/cancel": () => fail(409, "Задача уже выполняется или завершена"),
    }),
  );
  await renderAt("/documents", api);
  const queue = section("Очередь");
  await click(await within(queue).findByRole("button", { name: "Отменить" }));
  await click(within(queue).getByRole("button", { name: "Отменить задачу?" }));
  expect((await screen.findByRole("alert")).textContent).toContain("Задача уже выполняется или завершена");
});

test("«Очистить историю» скрывает завершённые; без них кнопки нет", async () => {
  const api = fakeApi(
    routes({
      [JOBS]: [job({ id: 1, status: "done", name: "a.pdf" }), job({ id: 2, status: "cancelled", name: "b.pdf" })],
      "POST /api/jobs/clear": { hidden: 2 },
    }),
  );
  await renderAt("/documents", api);
  const queue = section("Очередь");
  expect(await within(queue).findByText("отменена")).toBeTruthy();
  await click(within(queue).getByRole("button", { name: "Очистить историю" }));
  await click(within(queue).getByRole("button", { name: "Скрыть завершённые?" }));
  expect(api.requests()).toContain("POST /api/jobs/clear");
  await vi.waitFor(() => expect(count(api, JOBS)).toBe(2));
});

test("только упавшие и ожидающие — «Очистить историю» не показывается", async () => {
  const jobs = [job({ id: 1, status: "failed", error: "Boom" }), job({ id: 2, status: "pending", name: "b.pdf" })];
  await renderAt("/documents", fakeApi(routes({ [JOBS]: jobs })));
  const queue = section("Очередь");
  await within(queue).findByText("b.pdf");
  expect(within(queue).queryByRole("button", { name: "Очистить историю" })).toBeNull();
});

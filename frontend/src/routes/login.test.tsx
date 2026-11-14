import { createMemoryHistory } from "@tanstack/react-router";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { App, createQueryClient } from "../App";
import { createAppRouter } from "../router";
import { deferred, fail, fakeApi, type FakeApi } from "../test/fakeApi";

vi.mock("../graph/Graph2D", async () => ({ Graph2D: (await import("../test/fakeGraph")).FakeGraph2D }));
vi.mock("../graph/Graph3D", async () => ({ default: (await import("../test/fakeGraph")).FakeGraph3D }));

const ME = { id: "u1", username: "owner" };
const STATS = {
  documents: 1,
  documents_by_status: { done: 1 },
  chunks: 3,
  entities: 0,
  relations: 0,
  wiki_pages: 0,
  analysis: { ready: false, completed_at: null },
  recent: [],
};
const OVERVIEW = { nodes: [], relations: [], ranked_by: "mentions", truncated: false, total_nodes: 0 };
/** Данные главной: сводка, обзор графа, типы. */
const HOME = { "GET /api/stats": STATS, "GET /api/graph/overview": OVERVIEW, "GET /api/entities/types": [] };

async function renderAt(path: string, api: FakeApi) {
  vi.stubGlobal("fetch", api);
  const router = createAppRouter({ history: createMemoryHistory({ initialEntries: [path] }) });
  await router.load();
  const client = createQueryClient();
  render(<App router={router} client={client} />);
  return { router, client };
}

const rail = () => screen.queryByRole("navigation", { name: "Разделы" });

async function submitLogin(username = "owner", password = "correct horse battery") {
  fireEvent.change(screen.getByLabelText("Имя пользователя"), { target: { value: username } });
  fireEvent.change(screen.getByLabelText("Пароль"), { target: { value: password } });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Войти" })));
}

beforeAll(() => {
  window.scrollTo = vi.fn(); // jsdom не реализует scrollTo, роутер его зовёт
});
beforeEach(() => vi.stubEnv("VITE_FIXTURES", ""));
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

test("401 на странице — переход на /login с next, без рейки", async () => {
  const api = fakeApi({
    "GET /api/auth/me": () => fail(401, "Требуется вход"),
    "GET /api/stats": () => fail(401, "Требуется вход"),
  });
  const { router } = await renderAt("/documents?x=1", api);
  await vi.waitFor(() => expect(router.state.location.href).toBe("/login?next=%2Fdocuments%3Fx%3D1"));
  expect(await screen.findByRole("heading", { name: "mnogobase" })).toBeTruthy();
  expect(screen.getByText("Личная база знаний: документы, wiki и граф связей.")).toBeTruthy();
  expect(rail()).toBeNull();
  expect(screen.queryByRole("banner")).toBeNull();
});

test("401 на главной — тоже вход, next=/", async () => {
  const api = fakeApi({ "GET /api/auth/me": () => fail(401, "Требуется вход"), ...HOME, "GET /api/stats": () => fail(401, "x") });
  const { router } = await renderAt("/", api);
  await vi.waitFor(() => expect(router.state.location.href).toBe("/login?next=%2F"));
});

test("вход: «Вхожу…» во время запроса, затем next и /api/auth/me заново", async () => {
  const reply = deferred<Response>();
  let loggedIn = false;
  const api = fakeApi({
    "GET /api/auth/me": () => (loggedIn ? Response.json(ME) : fail(401, "Требуется вход")),
    "POST /api/auth/login": () => reply.promise,
  });
  const { router } = await renderAt("/documents?x=1", api);
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/login"));
  await screen.findByLabelText("Имя пользователя");

  await submitLogin();
  const busy = screen.getByRole("button", { name: "Вхожу…" }) as HTMLButtonElement;
  expect(busy.disabled).toBe(true);
  expect(api.bodies("POST", "/api/auth/login")).toEqual([
    { username: "owner", password: "correct horse battery", cookie: true },
  ]);

  loggedIn = true;
  await act(async () => reply.resolve(Response.json({ username: "owner" })));
  await vi.waitFor(() => expect(router.state.location.href).toBe("/documents?x=1"));
  await vi.waitFor(() =>
    expect(api.requests().filter((r) => r === "GET /api/auth/me")).toHaveLength(2),
  );
  expect(rail()).toBeTruthy();
  // замена записи истории: «Назад» не возвращает на форму входа
  expect(router.history.length).toBe(1);
});

test("401 при входе — «Неверное имя или пароль», остаёмся на форме", async () => {
  const api = fakeApi({ "POST /api/auth/login": () => fail(401, "Неверное имя пользователя или пароль") });
  const { router } = await renderAt("/login?next=%2Fwiki", api);
  await screen.findByLabelText("Имя пользователя");
  await submitLogin();
  expect((await screen.findByRole("alert")).textContent).toBe("Неверное имя или пароль");
  expect(router.state.location.href).toBe("/login?next=%2Fwiki");
  expect((screen.getByRole("button", { name: "Войти" }) as HTMLButtonElement).disabled).toBe(false);
});

test("429 при входе — текст detail сервера", async () => {
  const api = fakeApi({ "POST /api/auth/login": () => fail(429, "Слишком много одновременных проверок пароля") });
  await renderAt("/login", api);
  await screen.findByLabelText("Имя пользователя");
  await submitLogin();
  expect((await screen.findByRole("alert")).textContent).toBe("Слишком много одновременных проверок пароля");
});

test("чужой next после входа заменяется на /", async () => {
  const api = fakeApi({ "POST /api/auth/login": { username: "owner" }, "GET /api/auth/me": ME, ...HOME });
  const { router } = await renderAt("/login?next=%2F%2Fevil.example", api);
  await screen.findByLabelText("Имя пользователя");
  await submitLogin();
  await vi.waitFor(() => expect(router.state.location.href).toBe("/"));
});

test("«Выйти» — POST /api/auth/logout, /login и пустой кэш", async () => {
  const api = fakeApi({ "GET /api/auth/me": ME, ...HOME, "POST /api/auth/logout": { status: "ok" } });
  const { router, client } = await renderAt("/", api);
  await vi.waitFor(() => expect(api.requests()).toContain("GET /api/stats"));
  await act(async () => fireEvent.click(within(rail()!).getByRole("button", { name: "Выйти" })));
  await vi.waitFor(() => expect(router.state.location.href).toBe("/login"));
  expect(api.requests()).toContain("POST /api/auth/logout");
  expect(client.getQueryCache().getAll()).toHaveLength(0);
  expect(rail()).toBeNull();

  // кэш пуст: главная снова запрашивает сводку
  await act(async () => router.navigate({ to: "/" }));
  await vi.waitFor(() => expect(api.requests().filter((r) => r === "GET /api/stats")).toHaveLength(2));
});

test("ошибка выхода не мешает: всё равно /login", async () => {
  const api = fakeApi({ "GET /api/auth/me": ME, "POST /api/auth/logout": () => fail(503, "Сервис временно недоступен") });
  const { router } = await renderAt("/chat", api);
  await act(async () => fireEvent.click(within(rail()!).getByRole("button", { name: "Выйти" })));
  await vi.waitFor(() => expect(router.state.location.href).toBe("/login"));
});

test("рейка ведёт в аккаунт", async () => {
  const api = fakeApi({ "GET /api/auth/me": ME });
  await renderAt("/chat", api);
  const link = within(rail()!).getByRole("link", { name: "Аккаунт" });
  expect(link.getAttribute("href")).toBe("/account");
});

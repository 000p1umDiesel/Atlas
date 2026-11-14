import { createMemoryHistory } from "@tanstack/react-router";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { App, createQueryClient } from "../App";
import { createAppRouter } from "../router";
import { fail, fakeApi, type FakeApi } from "../test/fakeApi";

const ME = { id: "u1", username: "owner" };

async function renderAccount(api: FakeApi) {
  vi.stubGlobal("fetch", api);
  const router = createAppRouter({ history: createMemoryHistory({ initialEntries: ["/account"] }) });
  await router.load();
  render(<App router={router} client={createQueryClient()} />);
  await screen.findByText(/Вы вошли как/);
  return router;
}

function fill(current: string, next: string, repeat: string) {
  fireEvent.change(screen.getByLabelText("Текущий пароль"), { target: { value: current } });
  fireEvent.change(screen.getByLabelText("Новый пароль"), { target: { value: next } });
  fireEvent.change(screen.getByLabelText("Новый пароль ещё раз"), { target: { value: repeat } });
}

const submit = () => act(async () => fireEvent.submit(screen.getByRole("button", { name: "Сменить пароль" }).closest("form")!));
const input = (label: string) => screen.getByLabelText(label) as HTMLInputElement;

beforeAll(() => {
  window.scrollTo = vi.fn(); // jsdom не реализует scrollTo, роутер его зовёт
});
beforeEach(() => vi.stubEnv("VITE_FIXTURES", ""));
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

test("имя пользователя и подсказка о новых пользователях", async () => {
  await renderAccount(fakeApi({ "GET /api/auth/me": ME }));
  const intro = screen.getByText(/Вы вошли как/);
  expect(intro.textContent).toBe(
    "Вы вошли как owner. Новых пользователей создают на сервере командой mnogobase user create.",
  );
  expect(screen.getByText("mnogobase user create").tagName).toBe("CODE");
  expect(screen.getByText("После смены пароля остальные входы (другие браузеры, CLI-клиент) завершатся.")).toBeTruthy();
  expect(input("Новый пароль").minLength).toBe(12);
});

test("короткий новый пароль — сообщение без запроса", async () => {
  const api = fakeApi({ "GET /api/auth/me": ME });
  await renderAccount(api);
  fill("old password 1", "short", "short");
  await submit();
  expect(screen.getByRole("alert").textContent).toBe("Новый пароль — не короче 12 символов");
  expect(api.requests()).not.toContain("POST /api/auth/password");
});

test("новый пароль и повтор не совпадают — сообщение без запроса", async () => {
  const api = fakeApi({ "GET /api/auth/me": ME });
  await renderAccount(api);
  fill("old password 1", "new password 12", "new password 13");
  await submit();
  expect(screen.getByRole("alert").textContent).toBe("Новый пароль и повтор не совпадают");
  expect(api.requests()).not.toContain("POST /api/auth/password");
});

test("успех — «Пароль изменён.» и пустые поля", async () => {
  const api = fakeApi({ "GET /api/auth/me": ME, "POST /api/auth/password": { status: "ok" } });
  await renderAccount(api);
  fill("old password 1", "new password 12", "new password 12");
  await submit();
  expect((await screen.findByRole("status")).textContent).toBe("Пароль изменён.");
  expect(api.bodies("POST", "/api/auth/password")).toEqual([
    { old_password: "old password 1", new_password: "new password 12" },
  ]);
  for (const label of ["Текущий пароль", "Новый пароль", "Новый пароль ещё раз"]) expect(input(label).value).toBe("");
});

test("401 (неверный текущий пароль) — текст detail, без перехода на вход", async () => {
  const api = fakeApi({
    "GET /api/auth/me": ME,
    "POST /api/auth/password": () => fail(401, "Неверный текущий пароль"),
  });
  const router = await renderAccount(api);
  fill("wrong password", "new password 12", "new password 12");
  await submit();
  expect((await screen.findByRole("alert")).textContent).toBe("Неверный текущий пароль");
  await act(async () => new Promise((r) => setTimeout(r, 0)));
  expect(router.state.location.href).toBe("/account");
  expect(input("Текущий пароль").value).toBe("wrong password");
});

test("«Меняю…» и disabled во время запроса", async () => {
  let release!: (r: Response) => void;
  const api = fakeApi({
    "GET /api/auth/me": ME,
    "POST /api/auth/password": () => new Promise<Response>((done) => (release = done)),
  });
  await renderAccount(api);
  fill("old password 1", "new password 12", "new password 12");
  await submit();
  expect((screen.getByRole("button", { name: "Меняю…" }) as HTMLButtonElement).disabled).toBe(true);
  await act(async () => release(Response.json({ status: "ok" })));
  expect(await screen.findByRole("button", { name: "Сменить пароль" })).toBeTruthy();
});

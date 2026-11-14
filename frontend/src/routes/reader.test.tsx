import { createMemoryHistory } from "@tanstack/react-router";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { App, createQueryClient } from "../App";
import { createAppRouter } from "../router";
import { fakeApi, type FakeApi } from "../test/fakeApi";

const ME = { id: "u1", username: "owner" };
const DOC = { id: "d1", name: "Смит.pdf", title: "Богатство народов", n_pages: 9 };
const ch = (idx: number, headings: string[] = ["Глава 1"], page: number | null = 3) => ({
  chunk_id: `d1:${String(idx).padStart(5, "0")}`,
  idx,
  text: `Текст ${idx} <b>не разметка</b>`,
  headings,
  page_start: page,
  page_end: page,
});

const BASE = "GET /api/documents/d1/chunks?limit=50&offset=";
type Routes = Parameters<typeof fakeApi>[0];

async function renderAt(path: string, api: FakeApi) {
  vi.stubGlobal("fetch", api);
  const router = createAppRouter({ history: createMemoryHistory({ initialEntries: [path] }) });
  await router.load();
  render(<App router={router} client={createQueryClient()} />);
  return router;
}

const base = (extra: Routes = {}): Routes => ({ "GET /api/auth/me": ME, ...extra });

beforeAll(() => {
  window.scrollTo = vi.fn();
});
const scrollIntoView = vi.fn();
beforeEach(() => {
  vi.stubEnv("VITE_FIXTURES", "");
  Element.prototype.scrollIntoView = scrollIntoView;
  scrollIntoView.mockClear();
});
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

test("?chunk=73: запрос offset=50, фрагмент подсвечен и прокручен; «Дальше» — offset=100", async () => {
  const api = fakeApi(
    base({
      [`${BASE}50`]: { document: DOC, total: 120, chunks: [ch(72), ch(73), ch(74, ["Глава 2"], null)] },
      [`${BASE}100`]: { document: DOC, total: 120, chunks: [ch(100)] },
    }),
  );
  await renderAt("/documents/d1/read?chunk=73", api);
  const hit = await vi.waitFor(() => {
    const el = document.getElementById("chunk-73");
    if (!el) throw new Error("нет");
    return el;
  });
  expect(hit.className).toContain("hit");
  expect(document.getElementById("chunk-72")!.className).not.toContain("hit");
  expect(api.requests()).toContain(`${BASE}50`);
  expect(scrollIntoView).toHaveBeenCalledWith(expect.objectContaining({ block: "center" }));
  // текст — простым текстом, разметка не разбирается
  expect(hit.textContent).toContain("<b>не разметка</b>");
  expect(hit.textContent).toContain("с. 3");
  expect(document.getElementById("chunk-74")!.textContent).toContain("75");

  expect(screen.getAllByText("фрагменты 51–100 из 120", { exact: false })).toHaveLength(2);
  await act(async () => {
    fireEvent.click(screen.getAllByRole("button", { name: "Дальше" })[0]!);
    await new Promise((r) => setTimeout(r, 0));
  });
  expect(api.requests()).toContain(`${BASE}100`);
  expect(await screen.findByText("Текст 100", { exact: false })).toBeTruthy();
});

test("заголовок раздела один раз для подряд идущих фрагментов; пейджера нет при total <= 50", async () => {
  const api = fakeApi(
    base({ [`${BASE}0`]: { document: DOC, total: 3, chunks: [ch(0), ch(1), ch(2, ["Глава 2"])] } }),
  );
  await renderAt("/documents/d1/read", api);
  await screen.findByRole("heading", { level: 1, name: "Богатство народов" });
  expect(screen.getAllByRole("heading", { level: 3, name: "Глава 1" })).toHaveLength(1);
  expect(screen.getAllByRole("heading", { level: 3, name: "Глава 2" })).toHaveLength(1);
  expect(screen.queryByRole("button", { name: "Дальше" })).toBeNull();
  expect(screen.getByRole("link", { name: "Смит.pdf" }).getAttribute("href")).toBe("/documents/d1");
});

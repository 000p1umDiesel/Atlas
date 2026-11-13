import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { ApiError } from "./client";
import { shouldRetry, useOverview, useRecentDocuments, useStats } from "./queries";

const fetchMock = vi.fn();

const STATS = {
  documents: 2,
  documents_by_status: { done: 1, pending: 1 },
  chunks: 10,
  entities: 5,
  relations: 4,
  wiki_pages: 3,
  analysis: { ready: false, completed_at: null },
  recent: [{ id: "u1", name: "a.pdf", status: "pending", updated_at: null }],
};

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

beforeEach(() => {
  vi.stubEnv("VITE_FIXTURES", "");
  fetchMock.mockReset();
  fetchMock.mockImplementation(async () => Response.json(STATS));
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

test("сводка и последние документы — один запрос /api/stats", async () => {
  const Wrapper = wrapper();
  const { result } = renderHook(() => ({ stats: useStats(), recent: useRecentDocuments() }), { wrapper: Wrapper });
  await waitFor(() => expect(result.current.recent.data).toEqual(STATS.recent));
  expect(result.current.stats.data?.documents_by_status).toEqual({ done: 1, pending: 1 });
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(fetchMock.mock.calls[0]![0]).toBe("/api/stats");
});

test("обзор графа — /api/graph/overview?limit=500, ответ в {graph}", async () => {
  const payload = { nodes: [], relations: [], ranked_by: "mentions", truncated: false, total_nodes: 0 };
  fetchMock.mockImplementation(async () => Response.json(payload));
  const { result } = renderHook(() => useOverview(), { wrapper: wrapper() });
  await waitFor(() => expect(result.current.data).toEqual({ graph: payload }));
  expect(fetchMock.mock.calls[0]![0]).toBe("/api/graph/overview?limit=500");
});

test("повтор запроса: не при 401/404/422, иначе один раз", () => {
  for (const status of [401, 404, 422]) expect(shouldRetry(0, new ApiError(status, "x"))).toBe(false);
  expect(shouldRetry(0, new ApiError(503, "x"))).toBe(true);
  expect(shouldRetry(0, new TypeError("сеть"))).toBe(true);
  expect(shouldRetry(1, new ApiError(503, "x"))).toBe(false);
});

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import { ApiError } from "../api/client";
import { AuthNotice } from "./AuthNotice";

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <AuthNotice />
    </QueryClientProvider>,
  );
  return client;
}

async function fail(client: QueryClient, key: string, error: Error) {
  await act(async () => {
    await client.prefetchQuery({ queryKey: [key], queryFn: () => Promise.reject(error) });
  });
}

test("при 401 в кэше запросов видна ссылка на старый интерфейс", async () => {
  const client = setup();
  expect(screen.queryByRole("link", { name: /старый интерфейс/ })).toBeNull();
  await fail(client, "stats", new ApiError(401, "Требуется вход"));
  const link = screen.getByRole("link", { name: /старый интерфейс/ });
  expect(link.getAttribute("href")).toBe("/legacy");
  expect(screen.getByRole("status").textContent).toContain("Нужен вход");
});

test("без ошибок и при других ошибках плашки нет", async () => {
  const client = setup();
  await act(async () => {
    await client.prefetchQuery({ queryKey: ["ok"], queryFn: async () => 1 });
  });
  await fail(client, "missing", new ApiError(404, "Сущность не найдена"));
  await fail(client, "down", new Error("сеть"));
  expect(screen.queryByText(/Нужен вход/)).toBeNull();
});

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { useEffect } from "react";
import { onUnauthorized } from "./api/client";
import { shouldRetry } from "./api/queries";
import { createAppRouter } from "./router";

const defaultRouter = createAppRouter();

export function createQueryClient() {
  return new QueryClient({
    defaultOptions: { queries: { staleTime: 30_000, retry: shouldRetry, refetchOnWindowFocus: false } },
  });
}

const defaultClient = createQueryClient();

export function App({
  router = defaultRouter,
  client = defaultClient,
}: {
  router?: ReturnType<typeof createAppRouter>;
  client?: QueryClient;
}) {
  // Любой 401 (запрос, изменение, поток ответа, загрузка) — сессия кончилась: на вход, с возвратом
  // туда, где был пользователь. Ошибки 401 доходят сюда через api/client, а не через кэш Query:
  // так их видят и запросы мимо Query, а «тихие» 401 (неверный пароль) не уводят со страницы.
  useEffect(
    () =>
      onUnauthorized(() => {
        const { pathname, searchStr } = router.latestLocation;
        if (pathname === "/login") return; // уже на входе: не зацикливаемся
        void router.navigate({ to: "/login", search: { next: pathname + searchStr }, replace: true });
      }),
    [router],
  );
  return (
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  );
}

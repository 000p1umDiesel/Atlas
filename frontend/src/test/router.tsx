import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router";
import { render } from "@testing-library/react";
import type { ReactNode } from "react";
import { parsePlainSearch, stringifyPlainSearch } from "../lib/searchParams";

/**
 * Компонент внутри минимального роутера (любой путь подходит) и клиента Query — для
 * компонентов, которым нужны `usePanel` или переходы, без всей оболочки приложения.
 */
export async function renderWithRouter(ui: ReactNode, path = "/chat") {
  window.scrollTo = vi.fn(); // jsdom не реализует scrollTo, роутер его зовёт
  const root = createRootRoute({
    component: () => (
      <>
        {ui}
        <Outlet />
      </>
    ),
  });
  const any = createRoute({ getParentRoute: () => root, path: "$", component: () => null });
  const router = createRouter({
    routeTree: root.addChildren([any]),
    history: createMemoryHistory({ initialEntries: [path] }),
    parseSearch: parsePlainSearch,
    stringifySearch: stringifyPlainSearch,
  });
  await router.load();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router as never} />
    </QueryClientProvider>,
  );
  return { router, ...view };
}

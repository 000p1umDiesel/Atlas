import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { createAppRouter } from "./router";

const defaultRouter = createAppRouter();

export function createQueryClient() {
  return new QueryClient({
    defaultOptions: { queries: { staleTime: 30_000, retry: 1, refetchOnWindowFocus: false } },
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
  return (
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  );
}

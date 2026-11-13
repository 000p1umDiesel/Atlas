import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
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
  return (
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  );
}

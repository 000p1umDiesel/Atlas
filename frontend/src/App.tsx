import { RouterProvider } from "@tanstack/react-router";
import { createAppRouter } from "./router";

const defaultRouter = createAppRouter();

export function App({ router = defaultRouter }: { router?: ReturnType<typeof createAppRouter> }) {
  return <RouterProvider router={router} />;
}

import {
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  useRouterState,
  type RouterHistory,
} from "@tanstack/react-router";
import { MotionConfig } from "motion/react";
import { parseGraphSearch } from "./graph/search";
import { DesignPage } from "./routes/design";
import { Placeholder } from "./routes/placeholder";
import { Shell } from "./shell/Shell";

function Root() {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  return (
    <MotionConfig reducedMotion="user">
      <Shell pathname={pathname}>
        <Outlet />
      </Shell>
    </MotionConfig>
  );
}

const rootRoute = createRootRoute({ component: Root });

const page = (path: string, title: string) =>
  createRoute({ getParentRoute: () => rootRoute, path, component: () => <Placeholder title={title} /> });

const homeRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: () => <Placeholder title="Главная" />,
});

// Страница графа — заглушка до задачи 9; фильтры живут в search-параметрах.
const graphRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/graph",
  validateSearch: (raw: Record<string, unknown>) => parseGraphSearch(raw),
  component: () => <Placeholder title="Граф" />,
});

const designRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/_design",
  component: DesignPage,
});

const routeTree = rootRoute.addChildren([
  homeRoute,
  graphRoute,
  designRoute,
  page("/chat", "Чат"),
  page("/documents", "Документы"),
  page("/entities", "Сущности"),
  page("/wiki", "Wiki"),
  page("/status", "Статус"),
]);

export function createAppRouter(opts?: { history?: RouterHistory }) {
  return createRouter({ routeTree, history: opts?.history });
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof createAppRouter>;
  }
}

import {
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  useRouterState,
  type RouterHistory,
} from "@tanstack/react-router";
import { MotionConfig } from "motion/react";
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

// Task 6: сюда добавится validateSearch: parseGraphSearch и настоящая страница графа.
const graphRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/graph",
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

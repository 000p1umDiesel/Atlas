import {
  createRootRoute,
  createRoute,
  createRouter,
  lazyRouteComponent,
  Outlet,
  useRouterState,
  type RouterHistory,
} from "@tanstack/react-router";
import { MotionConfig } from "motion/react";
import type { ComponentType } from "react";
import { FilterPanel } from "./graph/FilterPanel";
import { parseGraphSearch, serializeGraphSearch } from "./graph/search";
import { parsePlainSearch, stringifyPlainSearch } from "./lib/searchParams";
import { DesignPage } from "./routes/design";
import { HomePage } from "./routes/home";
import { Placeholder } from "./routes/placeholder";
import { Shell } from "./shell/Shell";
import { RouteError } from "./ui/ErrorNotice";

function Root() {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  // Раскладку оболочки задаёт листовой маршрут через staticData.
  const Column = useRouterState({ select: (s) => s.matches[s.matches.length - 1]?.staticData.column });
  const bleed = useRouterState({ select: (s) => s.matches[s.matches.length - 1]?.staticData.bleed ?? false });
  return (
    <MotionConfig reducedMotion="user">
      <Shell pathname={pathname} column={Column ? <Column /> : null} bleed={bleed}>
        <Outlet />
      </Shell>
    </MotionConfig>
  );
}

const rootRoute = createRootRoute({ component: Root });

const page = <P extends string>(path: P, title: string) =>
  createRoute({ getParentRoute: () => rootRoute, path, component: () => <Placeholder title={title} /> });

const homeRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: HomePage,
});

// Фильтры графа живут в search-параметрах (простые строки, см. lib/searchParams).
// Маршрут хранит их в «чистом» виде — без дефолтов, списки через запятую; типизированный
// разбор делает useGraphSearch. Так в URL не попадает ничего, кроме serializeGraphSearch.
const graphRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/graph",
  validateSearch: (raw: Record<string, unknown>) => serializeGraphSearch(parseGraphSearch(raw)),
  staticData: { column: FilterPanel, bleed: true },
  // Sigma и graphology — отдельный чанк: грузятся только на странице графа.
  component: lazyRouteComponent(() => import("./routes/graph"), "GraphPage"),
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
  return createRouter({
    routeTree,
    history: opts?.history,
    // Русское сообщение вместо английского по умолчанию (в том числе для чанка прежней сборки).
    defaultErrorComponent: RouteError,
    // Без JSON: «123» остаётся строкой, списки — через запятую, дефолты не пишутся.
    parseSearch: parsePlainSearch,
    stringifySearch: stringifyPlainSearch,
  });
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof createAppRouter>;
  }
  interface StaticDataRouteOption {
    /** Содержимое контекстной колонки раздела. */
    column?: ComponentType;
    /** Страница во всю площадь, без полей. */
    bleed?: boolean;
  }
}

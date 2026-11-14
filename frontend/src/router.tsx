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
import { ConversationList } from "./chat/ConversationList";
import { EntitiesColumn } from "./entities/EntitiesColumn";
import { validateEntitiesSearch } from "./entities/search";
import { FilterPanel } from "./graph/FilterPanel";
import { parseGraphSearch, serializeGraphSearch } from "./graph/search";
import { parsePlainSearch, stringifyPlainSearch } from "./lib/searchParams";
import { AccountPage } from "./routes/account";
import { ChatPage } from "./routes/chat";
import { DesignPage } from "./routes/design";
import { DocumentPage } from "./routes/document";
import { DocumentsPage } from "./routes/documents";
import { EntitiesPage } from "./routes/entities";
import { EntityPage, validateEntitySearch } from "./routes/entity";
import { HomePage } from "./routes/home";
import { LoginPage } from "./routes/login";
import { ReaderPage } from "./routes/reader";
import { WikiPage } from "./routes/wiki";
import { WikiArticlePage } from "./routes/wikiPage";
import { StatusPage } from "./routes/status";
import { Shell } from "./shell/Shell";
import { NotFound, RouteError } from "./ui/ErrorNotice";

function Root() {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  // Раскладку оболочки задаёт листовой маршрут через staticData.
  const Column = useRouterState({ select: (s) => s.matches[s.matches.length - 1]?.staticData.column });
  const bleed = useRouterState({ select: (s) => s.matches[s.matches.length - 1]?.staticData.bleed ?? false });
  const bare = useRouterState({ select: (s) => s.matches[s.matches.length - 1]?.staticData.bare ?? false });
  return (
    <MotionConfig reducedMotion="user">
      {bare ? (
        <Outlet />
      ) : (
        <Shell pathname={pathname} column={Column ? <Column /> : null} bleed={bleed}>
          <Outlet />
        </Shell>
      )}
    </MotionConfig>
  );
}

const rootRoute = createRootRoute({ component: Root });

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

// Вход — без оболочки: рейка и колонка без сессии всё равно ничего не покажут.
const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  validateSearch: (raw: Record<string, unknown>): { next?: string } => ({
    next: typeof raw.next === "string" ? raw.next : undefined,
  }),
  staticData: { bare: true },
  component: LoginPage,
});

const accountRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/account",
  component: AccountPage,
});

// Чат: колонка — список диалогов; страница сама держит прокрутку истории и поле вопроса внизу.
// Search-параметры не проверяются: `panel` (правая панель) сохраняется как есть.
const chatRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/chat",
  staticData: { column: ConversationList, bleed: true },
  component: ChatPage,
});

const conversationRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/chat/$id",
  staticData: { column: ConversationList, bleed: true },
  component: ChatPage,
});

// Документы: загрузка, список и очередь. Search-параметры не проверяются: `panel` сохраняется.
const documentsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/documents",
  component: DocumentsPage,
});

// Карточка документа и чтение по фрагментам (`?chunk=<idx>`): параметры не проверяются, `panel` сохраняется.
const documentRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/documents/$id",
  component: DocumentPage,
});

const readerRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/documents/$id/read",
  component: ReaderPage,
});

// Каталог сущностей: поиск и фильтры в search-параметрах (простые строки, без дефолтов),
// колонка — типы и сообщества. Прочие параметры отбрасываются, `panel` сохраняется.
const entitiesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/entities",
  validateSearch: validateEntitiesSearch,
  staticData: { column: EntitiesColumn },
  component: EntitiesPage,
});

// Страница сущности: вкладка в `?tab=` (по умолчанию «Связи» не пишется), `panel` сохраняется.
// Колонка — та же, что у каталога (один раздел: переход сюда её не перемонтирует).
const entityRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/entities/$id",
  validateSearch: validateEntitySearch,
  staticData: { column: EntitiesColumn },
  component: EntityPage,
});

// Wiki: индекс с поиском (`?q`) и страницы по slug или id. `panel` и прочие параметры сохраняются.
const wikiRoute = createRoute({ getParentRoute: () => rootRoute, path: "/wiki", component: WikiPage });
const wikiSlugRoute = createRoute({ getParentRoute: () => rootRoute, path: "/wiki/$slug", component: WikiArticlePage });
const wikiIdRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/wiki/page/$pageId",
  component: WikiArticlePage,
});

const statusRoute = createRoute({ getParentRoute: () => rootRoute, path: "/status", component: StatusPage });

const designRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/_design",
  component: DesignPage,
});

const routeTree = rootRoute.addChildren([
  homeRoute,
  graphRoute,
  loginRoute,
  accountRoute,
  designRoute,
  chatRoute,
  conversationRoute,
  documentsRoute,
  documentRoute,
  readerRoute,
  entitiesRoute,
  entityRoute,
  wikiRoute,
  wikiSlugRoute,
  wikiIdRoute,
  statusRoute,
]);

export function createAppRouter(opts?: { history?: RouterHistory }) {
  return createRouter({
    routeTree,
    history: opts?.history,
    // Русское сообщение вместо английского по умолчанию (в том числе для чанка прежней сборки).
    defaultErrorComponent: RouteError,
    // Неизвестный адрес — русская страница внутри оболочки (корневой маршрут рисует Shell).
    defaultNotFoundComponent: NotFound,
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
    /** Без оболочки (рейки, колонки, верхней панели): страница входа. */
    bare?: boolean;
  }
}

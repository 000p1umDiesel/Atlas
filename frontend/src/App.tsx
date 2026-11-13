import { DesignPage } from "./routes/design";

// Временная заглушка до роутера (следующая задача): /_design показывает стилевую плитку.
export function App() {
  if (window.location.pathname === "/_design") return <DesignPage />;
  return (
    <main>
      <h1>mnogobase</h1>
    </main>
  );
}

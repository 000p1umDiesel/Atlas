import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { applyTheme, getThemeChoice } from "./lib/theme";
import "./styles/app.css";

// До первого рендера, чтобы не мигала чужая тема; тот же выбор затем читает useTheme.
applyTheme(getThemeChoice());

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

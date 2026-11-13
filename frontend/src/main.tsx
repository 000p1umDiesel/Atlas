import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { applyTheme, loadThemeChoice } from "./lib/theme";
import "./styles/app.css";

applyTheme(loadThemeChoice());

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

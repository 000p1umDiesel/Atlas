/// <reference types="vitest/config" />
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    // Сборка кладётся внутрь Python-пакета, откуда её раздаёт FastAPI.
    outDir: "../src/mnogobase/web/dist",
    emptyOutDir: true,
    // Никаких data: URL — иначе CSP страницы их не пропустит.
    assetsInlineLimit: 0,
  },
  // Каталога public/ нет: все ресурсы идут через импорты.
  publicDir: false,
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8080", changeOrigin: false },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    // Иначе Vitest отдаёт пустую строку и для `tokens.css?raw` (тест палитры читает токены).
    css: { include: [/tokens\.css/] },
  },
});

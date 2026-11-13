// Модульный воркер раскладки ForceAtlas2. Собирается Vite в отдельный файл
// assets/layout.worker-*.js: blob-воркеры CSP страницы не пропускает.
import type { LayoutFrame, LayoutRequest } from "./layoutProtocol";
import { createLayoutSession, type LayoutSession } from "./layoutSession";

let timer: ReturnType<typeof setTimeout> | null = null;
let session: LayoutSession | null = null;

function halt(): void {
  if (timer !== null) clearTimeout(timer);
  timer = null;
}

function tick(): void {
  timer = null;
  if (!session) return;
  const { positions, done } = session.step();
  const frame: LayoutFrame = { positions, done };
  self.postMessage(frame, { transfer: [positions.buffer] });
  // Пауза между пачками: даёт воркеру принять {kind: "stop"} и {kind: "pin"}.
  if (!done) timer = setTimeout(tick, 0);
}

self.onmessage = (e: MessageEvent<LayoutRequest>) => {
  const msg = e.data;
  if (msg.kind === "pin") {
    // Перетаскивание: сдвинуть закреплённый узел и (если раскладка уже сошлась) продолжить.
    session?.pin(msg.id, msg.x, msg.y);
    if (session && timer === null) timer = setTimeout(tick, 0);
    return;
  }
  halt();
  session = null;
  if (msg.kind !== "start") return;
  session = createLayoutSession(msg);
  tick();
};

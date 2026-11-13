// Модульный воркер раскладки ForceAtlas2. Собирается Vite в отдельный файл
// assets/layout.worker-*.js: blob-воркеры CSP страницы не пропускает.
import type { LayoutFrame, LayoutRequest } from "./layoutProtocol";
import { createLayoutSession } from "./layoutSession";

let timer: ReturnType<typeof setTimeout> | null = null;

function halt(): void {
  if (timer !== null) clearTimeout(timer);
  timer = null;
}

self.onmessage = (e: MessageEvent<LayoutRequest>) => {
  halt();
  if (e.data.kind !== "start") return;
  const session = createLayoutSession(e.data);
  const tick = () => {
    timer = null;
    const { positions, done } = session.step();
    const frame: LayoutFrame = { positions, done };
    self.postMessage(frame, { transfer: [positions.buffer] });
    // Пауза между пачками: даёт воркеру принять {kind: "stop"}.
    if (!done) timer = setTimeout(tick, 0);
  };
  tick();
};

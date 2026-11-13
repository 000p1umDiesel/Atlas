// Цветовая математика без DOM: OKLab/OKLCH, контраст WCAG и различимость при нарушениях
// цветовосприятия (симуляция Machado–Oliveira–Fernandes 2009, тяжесть 1.0 — та же модель,
// что у валидатора палитр dataviz). Нужна палитре сообществ и тестам токенов.

type Vec3 = [number, number, number];

function hexToSrgb(hex: string): Vec3 {
  const h = hex.trim().replace(/^#/, "");
  const full = h.length === 3 ? [...h].map((c) => c + c).join("") : h.slice(0, 6);
  const n = Number.parseInt(full, 16);
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
}

const toLinear = (c: number) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
const toGamma = (c: number) => (c <= 0.0031308 ? 12.92 * c : 1.055 * c ** (1 / 2.4) - 0.055);

function linearToOklab([r, g, b]: Vec3): Vec3 {
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  return [
    0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
  ];
}

function oklabToLinear([L, a, b]: Vec3): Vec3 {
  const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s = (L - 0.0894841775 * a - 1.291485548 * b) ** 3;
  return [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
  ];
}

const linear = (hex: string): Vec3 => hexToSrgb(hex).map(toLinear) as Vec3;

export function hexToOklab(hex: string): Vec3 {
  return linearToOklab(linear(hex));
}

/** [L, C, h°] */
export function hexToOklch(hex: string): Vec3 {
  const [L, a, b] = hexToOklab(hex);
  const h = ((Math.atan2(b, a) * 180) / Math.PI + 360) % 360;
  return [L, Math.hypot(a, b), h];
}

function inGamut(rgb: Vec3): boolean {
  return rgb.every((c) => c >= -1e-4 && c <= 1 + 1e-4);
}

function linearToHex(rgb: Vec3): string {
  return `#${rgb
    .map((c) => Math.round(toGamma(Math.min(1, Math.max(0, c))) * 255).toString(16).padStart(2, "0"))
    .join("")}`;
}

/**
 * OKLCH → `#rrggbb`. Цвет вне охвата sRGB ужимается по хроме (двоичный поиск) при той же
 * светлоте и оттенке: так соседние цвета не схлопываются в одну обрезанную точку.
 */
export function oklchToHex(L: number, C: number, hueDeg: number): string {
  const rad = (hueDeg * Math.PI) / 180;
  const at = (c: number) => oklabToLinear([L, c * Math.cos(rad), c * Math.sin(rad)]);
  let rgb = at(C);
  if (!inGamut(rgb)) {
    let lo = 0;
    let hi = C;
    for (let i = 0; i < 24; i++) {
      const mid = (lo + hi) / 2;
      if (inGamut(at(mid))) lo = mid;
      else hi = mid;
    }
    rgb = at(lo);
  }
  return linearToHex(rgb);
}

function luminance(hex: string): number {
  const [r, g, b] = linear(hex);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** Контраст WCAG 2 (1…21). */
export function contrastRatio(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/** Расстояние в OKLab ×100 (нормальное зрение). */
export function deltaE(a: string, b: string): number {
  const p = hexToOklab(a);
  const q = hexToOklab(b);
  return 100 * Math.hypot(p[0] - q[0], p[1] - q[1], p[2] - q[2]);
}

const MACHADO: Record<"protan" | "deutan", [Vec3, Vec3, Vec3]> = {
  protan: [
    [0.152286, 1.052583, -0.204868],
    [0.114503, 0.786281, 0.099216],
    [-0.003882, -0.048116, 1.051998],
  ],
  deutan: [
    [0.367322, 0.860646, -0.227968],
    [0.280085, 0.672501, 0.047413],
    [-0.01182, 0.04294, 0.968881],
  ],
};

function simulate(hex: string, kind: "protan" | "deutan"): Vec3 {
  const [r, g, b] = linear(hex);
  const clamp = (c: number) => Math.min(1, Math.max(0, c));
  return MACHADO[kind].map(([x, y, z]) => clamp(x * r + y * g + z * b)) as Vec3;
}

/** Расстояние в OKLab ×100 между цветами, как их видит человек с протанопией/дейтеранопией. */
export function cvdDeltaE(a: string, b: string, kind: "protan" | "deutan"): number {
  const p = linearToOklab(simulate(a, kind));
  const q = linearToOklab(simulate(b, kind));
  return 100 * Math.hypot(p[0] - q[0], p[1] - q[1], p[2] - q[2]);
}

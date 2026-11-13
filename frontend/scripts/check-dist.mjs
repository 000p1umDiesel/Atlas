// Проверка собранной страницы: CSP запрещает inline-скрипты, стили и data: URL.
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const dist = join(dirname(fileURLToPath(import.meta.url)), "../../src/mnogobase/web/dist");
const errors = [];

const indexPath = join(dist, "index.html");
if (!existsSync(indexPath)) {
  console.error(`Сборка не найдена: ${indexPath}`);
  process.exit(1);
}
const html = readFileSync(indexPath, "utf8");
if (/<script(?![^>]*\ssrc=)[^>]*>/i.test(html)) errors.push("в index.html есть inline-<script> без src");
if (/<style/i.test(html)) errors.push("в index.html есть тег <style>");
if (/\sstyle=/i.test(html)) errors.push("в index.html есть атрибут style=");

const assets = join(dist, "assets");
if (existsSync(assets)) {
  for (const name of readdirSync(assets).filter((n) => n.endsWith(".css"))) {
    if (/url\(\s*["']?data:/i.test(readFileSync(join(assets, name), "utf8"))) {
      errors.push(`в assets/${name} есть url(data:...) — ресурс встроен в CSS`);
    }
  }
}

if (errors.length) {
  for (const e of errors) console.error(`Нарушение CSP: ${e}`);
  process.exit(1);
}
console.log("check-dist: ок, inline-кода в сборке нет");

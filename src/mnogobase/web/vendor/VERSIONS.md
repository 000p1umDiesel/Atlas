# Сторонние файлы веб-интерфейса

Скачаны с https://cdn.jsdelivr.net/npm/ и лежат в репозитории как есть: в браузер ничего не
грузится с CDN (CSP `default-src 'self'`). Обновление: скачать новую версию по тому же пути,
пересчитать sha256 (`shasum -a 256 vendor/*.mjs fonts/*.woff2`) и поправить таблицу.

| Файл | Пакет и путь | Лицензия |
|---|---|---|
| `vendor/htm-preact-standalone.mjs` | `htm@3.1.1/preact/standalone.module.js` (Preact + hooks + htm, без внешних импортов) | Apache-2.0 (htm), MIT (Preact) |
| `vendor/markdown-it.mjs` | `markdown-it@15.0.3/dist/browser/markdown-it.esm.min.mjs` | MIT |
| `vendor/purify.mjs` | `dompurify@3.4.17/dist/purify.es.mjs` | MPL-2.0 OR Apache-2.0 |
| `vendor/cytoscape.mjs` | `cytoscape@3.34.4/dist/cytoscape.esm.min.mjs` | MIT |
| `fonts/literata-*.woff2` | `@fontsource-variable/literata@5.3.0/files/` (cyrillic, latin, greek; normal и italic) | OFL-1.1 (`fonts/LICENSE-literata.txt`) |
| `fonts/golos-text-*.woff2` | `@fontsource-variable/golos-text@5.3.0/files/` (cyrillic, latin) | OFL-1.1 (`fonts/LICENSE-golos-text.txt`) |

## sha256

```
0d7e373a706f0f01d6fb7a6ccf04988c01307cee1c2f9bcc7b068e9f6b431e3f  vendor/cytoscape.mjs
72284e8e9079c87817145df1110f74e8a2aa040b2fc384922e18dfcb46fc1fd7  vendor/htm-preact-standalone.mjs
d5432491790141da046b1197353705db03264f40b5727f3b4b591f5aba193e30  vendor/markdown-it.mjs
60c5d5ccbb466de3de3ba863461a92bf134f8c156cef7992da43e0f659bc5cbd  vendor/purify.mjs
17d048ca05cb1218af3c0d6dcdf882989e6d1cc5dcb598ea50eaf54850ff7229  fonts/golos-text-cyrillic-wght-normal.woff2
9a69d0aa4734c4022224c002a3d944a702e0204972a49d892789f5668b922c2a  fonts/golos-text-latin-wght-normal.woff2
62e8c9a48487878d0027e5946773f029f28ec6be99f1f20eb26b6aa401395c29  fonts/literata-cyrillic-wght-italic.woff2
df20f1a8ca3c15497861c6dbf36c0c59507ee351301527ec89543747067ad4d7  fonts/literata-cyrillic-wght-normal.woff2
393643556e420af45f8f0854c30f00e17ac52e85f1df4fd4ae2f2a29e0d279f1  fonts/literata-greek-wght-normal.woff2
ab198d6616c7cc966f26a4a5b28a3977dc47439640f09d9b3361226bd465c404  fonts/literata-latin-wght-italic.woff2
9adbeac5b167fe5ad6c49d9e29aa0c76e2f1bb3b46bf4ebf12a9eca7d3525384  fonts/literata-latin-wght-normal.woff2
```

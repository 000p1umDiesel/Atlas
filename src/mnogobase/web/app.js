// Точка входа веб-интерфейса mnogobase (каркас; экраны добавляются следующими задачами).
import { html, render } from "/static/vendor/htm-preact-standalone.mjs";

render(html`<main class="boot">mnogobase</main>`, document.getElementById("app"));

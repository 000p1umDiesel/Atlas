// Маршруты внутри страницы: #/chat/<id>, #/documents/<id>/view?chunk=..., #/wiki/<slug> ...
// Сервер их не видит, поэтому ему достаточно отдавать один index.html.
import { useEffect, useState } from "/static/vendor/htm-preact-standalone.mjs";

export function parseHash(hash = location.hash) {
  const raw = hash.replace(/^#\/?/, "");
  const [pathPart, queryPart = ""] = raw.split("?");
  const parts = pathPart.split("/").filter(Boolean).map(decodeURIComponent);
  return { parts: parts.length ? parts : ["chat"], query: new URLSearchParams(queryPart) };
}

export function useRoute() {
  const [route, setRoute] = useState(parseHash());
  useEffect(() => {
    const update = () => setRoute(parseHash());
    addEventListener("hashchange", update);
    return () => removeEventListener("hashchange", update);
  }, []);
  return route;
}

export function link(...parts) {
  return "#/" + parts.map((p) => encodeURIComponent(p)).join("/");
}

export function navigate(href) {
  if (location.hash !== href) location.hash = href;
}

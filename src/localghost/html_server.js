// Served by html_server.py as /__localghost/reload.js. Each event lists the
// changed files' URL paths, one per line. Changes this page never loaded are
// ignored; changed stylesheets it links are swapped in place, keeping scroll,
// form and script state; anything else reloads the page.
performance.setResourceTimingBufferSize(1000);
new EventSource("/__localghost/reload").onmessage = (event) => {
  const page = location.pathname;
  const used = new Set(
    page.endsWith("/") ? [page + "index.html", page + "index.htm"] : [page],
  );
  for (const entry of performance.getEntriesByType("resource")) {
    const url = new URL(entry.name);
    if (url.origin === location.origin) used.add(url.pathname);
  }
  const changed = event.data.split("\n").filter((path) => used.has(path));
  if (!changed.length) return;
  const links = [...document.querySelectorAll("link[rel=stylesheet]")].filter(
    (link) => changed.includes(new URL(link.href).pathname),
  );
  const linked = new Set(links.map((link) => new URL(link.href).pathname));
  if (!changed.every((path) => linked.has(path))) return location.reload();
  for (const old of links) {
    // The new sheet loads before the old one goes, so nothing flashes unstyled.
    const link = old.cloneNode();
    link.href = new URL(old.href).pathname + "?" + Date.now();
    link.onload = () => old.remove();
    old.after(link);
  }
};

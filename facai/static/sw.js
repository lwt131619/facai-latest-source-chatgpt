// FACAI service worker: installable PWA shell with network-first delivery.
// The console is live data — never serve a stale API response. Only the
// static shell (login/console HTML) is cached, and only after a successful
// network response.
const SHELL = ["/", "/login", "/static/mobile.html", "/static/facai-ui.css", "/static/mobile.webmanifest", "/static/icon-192.png", "/static/icon-512.png"];
const CACHE = "facai-shell-v2";

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => Promise.all(SHELL.map((path) => c.add(path).catch(() => null)))).catch(() => null));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // API/websocket-ish traffic: straight through, never cached.
  if (url.pathname.startsWith("/api/")) return;

  // Static shell: network-first, cache as fallback for offline shell.
  if (SHELL.includes(url.pathname)) {
    event.respondWith(
      fetch(req)
        .then((res) => {
          if (res.ok) {
            const clone = res.clone();
            caches.open(CACHE).then((c) => c.put(req, clone)).catch(() => null);
          }
          return res;
        })
        .catch(() => caches.match(req).then((hit) => hit || Response.error()))
    );
  }
});

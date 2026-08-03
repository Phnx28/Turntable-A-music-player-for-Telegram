// Minimal app-shell cache: the UI is tiny and static, so offline boot costs little and the
// service worker adds real value. Audio, covers and every /api/ response are explicitly NOT
// cached -- those are large, private, and change constantly; caching them here would serve
// stale bytes and leak library contents into the caches API.

const SHELL = "/";
const SHELL_CACHE = "turntable-shell-v1";

const SHELL_ASSETS = [
  "/",
  "/assets/app.js",
  "/assets/style.css",
  "/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE)
      .then((cache) => cache.addAll(SHELL_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== SHELL_CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== location.origin) return;
  // The API is the whole product; the worker must never intercept it.
  if (url.pathname.startsWith("/api/")) return;
  if (url.pathname === "/sw.js") return;

  if (request.mode === "navigate") {
    // Network-first: a restart or an edit must be visible immediately; the cached shell is
    // only for when the server is unreachable.
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(SHELL_CACHE).then((cache) => cache.put(SHELL, copy)).catch(() => {});
          return response;
        })
        .catch(() => caches.match(SHELL).then((cached) => cached || Response.error()))
    );
    return;
  }

  // Static assets: stale-while-revalidate. The server already sends no-cache + ETag, so this
  // only speeds up repeat loads.
  event.respondWith(
    caches.match(request).then((cached) => {
      const update = fetch(request)
        .then((response) => {
          if (response.ok && url.pathname.startsWith("/assets/")) {
            const copy = response.clone();
            caches.open(SHELL_CACHE).then((cache) => cache.put(request, copy)).catch(() => {});
          }
          return response;
        })
        .catch(() => cached);
      return cached || update;
    })
  );
});

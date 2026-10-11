// Sunny Side service worker. It makes the site installable and shows a friendly page when
// the phone is offline. Pages and API answers are never cached: they hold one person's
// jobs and inbox, and a shared phone or a later sign-in must never see them.
const CACHE = "sunny-shell-v1";
const SHELL = ["/offline", "/logo.svg", "/static/icons/icon-192.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET" || new URL(req.url).origin !== self.location.origin) return;
  if (req.mode === "navigate") {
    event.respondWith(fetch(req).catch(() => caches.match("/offline")));
    return;
  }
  if (SHELL.includes(new URL(req.url).pathname)) {
    event.respondWith(caches.match(req).then((hit) => hit || fetch(req)));
  }
});

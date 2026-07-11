/* Service worker.

   Two strategies, because the two kinds of request want opposite things:

   * The app shell (HTML/CSS/JS) is cache-first — it changes only on deploy, and
     serving it from cache is what makes the app open instantly offline.
   * The data (/api/data, /api/enrichment/all) is network-first with a cache
     fallback — you always want today's spreadsheet if the network can give it,
     but yesterday's is far better than an error screen on the train.

   The cache name carries the build version, so a deploy evicts the old shell
   rather than serving stale JS forever. */

const VERSION = "v0.75.0";
const SHELL = `gamedex-shell-${VERSION}`;
const DATA = `gamedex-data-${VERSION}`;

const SHELL_URLS = [
  "./", "./index.html", "./style.css", "./app.js", "./charts.js", "./home.js",
  "./reviews.js", "./health.js", "./collections.js", "./challenges.js",
  "./franchise.js", "./timeline.js", "./extras.js", "./predict.js", "./relations.js",
  "./manifest.webmanifest", "./icon.svg",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_URLS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys
        .filter((k) => k !== SHELL && k !== DATA)
        .map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;

  // Never cache a mutation or a live status poll.
  if (url.pathname.includes("/api/refresh") || url.pathname.includes("/api/enrichment/stats")) return;

  // Data: network first, fall back to the last good copy.
  if (url.pathname.startsWith("/api/")) {
    e.respondWith(
      fetch(e.request)
        .then((res) => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(DATA).then((c) => c.put(e.request, copy));
          }
          return res;
        })
        .catch(() => caches.match(e.request).then((hit) => hit || Response.error()))
    );
    return;
  }

  // Cover art: cache-first, it never changes for a given URL.
  if (/images\.igdb\.com|images\.launchbox-app\.com|t\.vndb\.org|adb\.arcadeitalia\.net/.test(url.host)) {
    e.respondWith(
      caches.match(e.request).then((hit) => hit || fetch(e.request).then((res) => {
        if (res.ok) { const copy = res.clone(); caches.open(DATA).then((c) => c.put(e.request, copy)); }
        return res;
      }).catch(() => hit))
    );
    return;
  }

  // Shell: cache first, revalidate in the background.
  if (url.origin === self.location.origin) {
    e.respondWith(
      caches.match(e.request).then((hit) => {
        const net = fetch(e.request).then((res) => {
          if (res.ok) { const copy = res.clone(); caches.open(SHELL).then((c) => c.put(e.request, copy)); }
          return res;
        }).catch(() => hit);
        return hit || net;
      })
    );
  }
});

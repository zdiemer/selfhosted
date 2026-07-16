/* Service worker.

   Two strategies, because the two kinds of request want opposite things:

   * The app shell (HTML/CSS/JS) is cache-first — it changes only on deploy, and
     serving it from cache is what makes the app open instantly offline.
   * The data (/api/data, /api/enrichment/all) is network-first with a cache
     fallback — you always want today's spreadsheet if the network can give it,
     but yesterday's is far better than an error screen on the train.

   The cache name carries the build version, so a deploy evicts the old shell
   rather than serving stale JS forever. */

const VERSION = "v1.39.0";
const SHELL = `gamedex-shell-${VERSION}`;
const DATA = `gamedex-data-${VERSION}`;

const SHELL_URLS = [
  "./", "./index.html", "./style.css", "./manifest.webmanifest", "./icon.svg",
  "./app.js", "./attract.js", "./challenges.js", "./charts.js", "./collections.js", "./extras.js",
  "./groups.js", "./health.js", "./home.js", "./konami.js", "./media.js",
  "./picross.js", "./predict.js", "./relations.js", "./shelf.js", "./timeline.js",
  "./fonts/archivo-800.woff2", "./fonts/plex-sans.woff2",
];

// Cache each URL on its own. addAll() is atomic — one 404 rejects the whole
// batch, the install fails, and the app silently has NO offline cache at all.
// That is exactly what happened when reviews.js was deleted in 1.11.4 and this
// list kept asking for it: sixteen releases with a dead service worker and no
// symptom, because online everything still works.
self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(SHELL)
      .then((c) => Promise.all(SHELL_URLS.map((u) =>
        c.add(u).catch((err) => console.warn("sw: skipped", u, err)))))
      .then(() => self.skipWaiting())
  );
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

  // Server-cached images (/api/img proxy, /api/shelf/face box cuts): the bytes for a
  // given URL never change, so cache-first with no revalidation — one hit on the
  // network, everything after off the device. This MUST precede the generic /api/
  // branch below, which is network-first and would defeat the point. Keeping a
  // browser copy on top of the PVC cache means a repeat view costs neither a CDN
  // fetch nor a round-trip to the pod.
  if (url.pathname.startsWith("/api/img") || url.pathname.startsWith("/api/manual")
      || url.pathname.startsWith("/api/shelf/face")) {
    e.respondWith(
      caches.match(e.request).then((hit) => hit || fetch(e.request).then((res) => {
        if (res.ok) { const copy = res.clone(); caches.open(DATA).then((c) => c.put(e.request, copy)); }
        return res;
      }))
    );
    return;
  }

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

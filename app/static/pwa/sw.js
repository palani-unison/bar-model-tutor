// Service worker for "Add to Home Screen" (app/static/pwa). The tutor needs the network for every lesson
// (planner, voice, live WebSocket), so this only keeps the page shell and fonts for a fast start and a
// friendly offline message. API calls, lessons and videos always go to the network and are never cached.
const CACHE = "bmt-shell-v1";
const SHELL = ["/", "/pwa/icon-192.png", "/fonts/Caveat.ttf", "/fonts/PatrickHand-Regular.ttf"];
const OFFLINE = "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>" +
  "<body style='margin:0;display:grid;place-items:center;height:100vh;background:#0b0d12;color:#f1f5f9;" +
  "font:20px system-ui;text-align:center;padding:16px'>You're offline.<br>Lessons need the internet — " +
  "try again when you're connected.</body>";

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws") || url.pathname.startsWith("/out/")) return;
  if (e.request.mode === "navigate") {
    // Network first, so a deploy shows up at once; the cached page only when offline.
    e.respondWith(fetch(e.request).then((res) => {
      if (url.pathname === "/" && res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put("/", copy)); }
      return res;
    }).catch(() => caches.match("/").then((hit) => hit ||
      new Response(OFFLINE, { headers: { "Content-Type": "text/html; charset=utf-8" } }))));
    return;
  }
  if (url.pathname.startsWith("/fonts/") || url.pathname.startsWith("/pwa/")) {
    e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request)));
  }
});

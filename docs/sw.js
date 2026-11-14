// Minimaler Service Worker: leitet jede Anfrage unverändert weiter und speichert nichts,
// damit die Daten immer frisch bleiben. Er ist nur da, damit Chrome auf dem Handy die Seite als App installieren kann.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", e => e.respondWith(fetch(e.request)));

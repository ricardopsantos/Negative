/* Photos service worker (PH87): only makes the web app installable, so Android's share sheet can offer it.
   No fetch handler on purpose: nothing is cached, every request goes to the server as before. */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));

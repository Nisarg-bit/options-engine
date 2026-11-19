/* Service worker for the installable desk.
 *
 * Rules (market data must never be stale):
 *   - only same-origin GET requests are handled; the Realtime Database (REST and
 *     EventSource) is on another origin and always goes straight to the network;
 *   - Firebase's reserved /__/ paths are never touched;
 *   - everything else is network-first: a fresh response is used and cached, and
 *     the cache is only a fallback so the app still opens offline.
 * Bump VERSION to drop old caches after a big change.
 */
const VERSION = 'desk-v6';
const SHELL = [
  '/', '/index.html', '/desk.css', '/studio.css', '/desk.js', '/guide.js', '/studio.js', '/pulse.css', '/pulse.js', '/options.js', '/futures.css', '/futures.js', '/futures-market-demo.json', '/expand.js', '/pwa.js',
  '/manifest.webmanifest', '/icons/icon-192.png', '/icons/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(VERSION)
      .then((cache) => Promise.allSettled(SHELL.map((url) => cache.add(new Request(url, { cache: 'reload' })))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/__/')) return;
  if ((req.headers.get('accept') || '').includes('text/event-stream')) return;

  event.respondWith((async () => {
    try {
      const fresh = await fetch(req);
      if (fresh.ok && fresh.type === 'basic') {
        const cache = await caches.open(VERSION);
        cache.put(req, fresh.clone());
      }
      return fresh;
    } catch (err) {
      const cached = await caches.match(req, { ignoreSearch: req.mode === 'navigate' });
      if (cached) return cached;
      if (req.mode === 'navigate') {
        const shell = await caches.match('/index.html');
        if (shell) return shell;
      }
      throw err;
    }
  })());
});

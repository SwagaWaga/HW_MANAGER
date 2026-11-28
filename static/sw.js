const SW_VERSION = 'v1.0.6';
const SHELL_CACHE = `hw-portal-shell-${SW_VERSION}`;
const RUNTIME_CACHE = `hw-portal-runtime-${SW_VERSION}`;
const API_CACHE = `hw-portal-api-${SW_VERSION}`;
const MEDIA_CACHE = `hw-portal-media-${SW_VERSION}`;

const VALID_CACHES = new Set([SHELL_CACHE, RUNTIME_CACHE, API_CACHE, MEDIA_CACHE]);

const PRECACHE_URLS = [
  '/',
  '/manifest.webmanifest',
  '/static/pwa/icon-192.png',
  '/static/pwa/icon-512.png',
  '/static/pwa/icon-maskable-512.png',
  '/static/pwa/apple-touch-icon.png',
  '/static/pwa/favicon-32.png',
  '/static/schedule_10a.webp'
];

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const shell = await caches.open(SHELL_CACHE);
    await Promise.allSettled(
      PRECACHE_URLS.map((url) => shell.add(new Request(url, { cache: 'reload' })))
    );
    const runtime = await caches.open(RUNTIME_CACHE);
    await Promise.allSettled([
      runtime.add(new Request('https://cdn.tailwindcss.com/3.4.17', { mode: 'no-cors' }))
    ]);
  })());
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(
      keys.filter((k) => !VALID_CACHES.has(k)).map((k) => caches.delete(k))
    );
    await self.clients.claim();
  })());
});

self.addEventListener('message', (event) => {
  const data = event.data || {};
  if (data.type === 'SKIP_WAITING') {
    self.skipWaiting();
    return;
  }
  if (data.type === 'CLEAR_PWA_CACHE') {
    event.waitUntil(
      Promise.all([caches.delete(API_CACHE), caches.delete(RUNTIME_CACHE)])
    );
  }
});

function isCacheableResponse(res) {
  return Boolean(res && (res.status === 200 || res.type === 'opaque'));
}

async function networkFirst(request, cacheName, fallbackKey = null) {
  try {
    const res = await fetch(request);
    if (isCacheableResponse(res) && !res.redirected) {
      const cache = await caches.open(cacheName);
      cache.put(fallbackKey || request, res.clone()).catch(() => {});
    }
    return res;
  } catch (err) {
    const cache = await caches.open(cacheName);
    const cached = (await cache.match(fallbackKey || request)) || (await caches.match(request));
    if (cached) return cached;
    throw err;
  }
}

async function cacheFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(request);
  if (cached) return cached;
  const res = await fetch(request);
  if (isCacheableResponse(res) && !res.redirected) {
    cache.put(request, res.clone()).catch(() => {});
  }
  return res;
}

async function staleWhileRevalidate(request, cacheName, event) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(request);
  const networkPromise = fetch(request)
    .then((res) => {
      if (isCacheableResponse(res) && (!res.redirected || res.type === 'opaque' || res.type === 'cors')) {
        cache.put(request, res.clone()).catch(() => {});
      }
      return res;
    })
    .catch(() => null);

  if (cached) {
    if (event && event.waitUntil) {
      event.waitUntil(networkPromise);
    }
    return cached;
  }
  const net = await networkPromise;
  if (net) return net;
  return new Response('', { status: 503, statusText: 'Offline' });
}

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return;

  // 0. Файловые загрузки, админ-аналитика и Range-запросы не перехватываются SW
  if (
    url.origin === self.location.origin &&
    (
      url.pathname.startsWith('/api/admin/') ||
      url.pathname.startsWith('/api/analytics/') ||
      url.pathname.endsWith('/download') ||
      url.searchParams.get('download') === '1' ||
      request.headers.has('range')
    )
  ) {
    return;
  }

  // 1. HTML-навигация — Network-First с оффлайн-фолбэком только для HTML-страницы (не для /api/ и /uploads/)
  const isPageNavigation =
    (request.mode === 'navigate' && !url.pathname.startsWith('/api/') && !url.pathname.startsWith('/uploads/')) ||
    (url.origin === self.location.origin && url.pathname === '/');
  if (isPageNavigation) {
    event.respondWith(
      networkFirst(request, SHELL_CACHE, '/').catch(async () => {
        const shell = await caches.open(SHELL_CACHE);
        return (await shell.match('/')) || new Response('Оффлайн. Подключитесь к интернету.', {
          status: 503,
          headers: { 'Content-Type': 'text/plain; charset=utf-8' }
        });
      })
    );
    return;
  }

  // 2. Same-origin API и загруженные материалы
  if (url.origin === self.location.origin) {
    if (url.pathname.startsWith('/api/materials/') || url.pathname.startsWith('/uploads/')) {
      // Изображения и миниатюры с immutable ETag отдаются Cache-First за 0ms
      if (request.destination === 'image') {
        event.respondWith(
          cacheFirst(request, MEDIA_CACHE).catch(() =>
            new Response('', { status: 503, statusText: 'Offline' })
          )
        );
      } else {
        event.respondWith(
          networkFirst(request, MEDIA_CACHE).catch(() =>
            new Response('Материал недоступен оффлайн', { status: 503 })
          )
        );
      }
      return;
    }

    if (url.pathname.startsWith('/api/') || url.pathname === '/health' || url.pathname === '/ping') {
      event.respondWith(
        networkFirst(request, API_CACHE).catch(() => {
          if (url.pathname === '/api/status' || url.pathname === '/health' || url.pathname === '/ping') {
            return new Response(JSON.stringify({ status: 'offline', backend: 'cache' }), {
              status: 200,
              headers: { 'Content-Type': 'application/json; charset=utf-8' }
            });
          }
          return new Response(JSON.stringify({ detail: 'Offline', offline: true }), {
            status: 503,
            headers: { 'Content-Type': 'application/json; charset=utf-8' }
          });
        })
      );
      return;
    }

    if (
      url.pathname.startsWith('/static/') ||
      url.pathname === '/manifest.webmanifest' ||
      url.pathname === '/favicon.ico'
    ) {
      event.respondWith(staleWhileRevalidate(request, SHELL_CACHE, event));
      return;
    }
  }

  // 3. Внешние CDN (Tailwind, Google Fonts, Telegram WebApp SDK)
  if (
    url.hostname === 'fonts.googleapis.com' ||
    url.hostname === 'fonts.gstatic.com' ||
    url.hostname === 'cdn.tailwindcss.com'
  ) {
    event.respondWith(staleWhileRevalidate(request, RUNTIME_CACHE, event));
    return;
  }

  if (url.hostname === 'telegram.org') {
    event.respondWith(
      staleWhileRevalidate(request, RUNTIME_CACHE, event).catch(() =>
        new Response('', {
          status: 200,
          headers: { 'Content-Type': 'application/javascript; charset=utf-8' }
        })
      )
    );
  }
});

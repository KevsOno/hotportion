// ─── HOT PORTION GRILL SERVICE WORKER ───
// Version: 2.0.0
const CACHE_NAME = 'hotportion-v2';
const STATIC_ASSETS = [
  '/',
  '/index.html',
  '/admin.html',
  '/fastfood/manifest.json',
  '/fastfood/icons/icon-192.png',
  '/fastfood/icons/icon-512.png',
  '/fastfood/icons/icon-512-maskable.png',
  '/js/seo.js'
];

// ─── Install: Pre-cache critical assets ───
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => {
        console.log('📦 Caching static assets...');
        return cache.addAll(STATIC_ASSETS).catch((err) => {
          console.warn('⚠️ Some assets failed to cache:', err);
        });
      })
      .then(() => self.skipWaiting())
  );
});

// ─── Activate: Clean up old caches ───
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.map((name) => {
          if (name !== CACHE_NAME) {
            console.log('🧹 Removing old cache:', name);
            return caches.delete(name);
          }
        })
      );
    }).then(() => self.clients.claim())
  );
});

// ─── Fetch: Network-first with fallback to cache ───
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Skip non-GET requests, cross-origin fonts, and analytics
  if (event.request.method !== 'GET') return;
  if (url.pathname.includes('/api/')) return; // Don't cache API calls
  if (url.pathname.includes('/supabase')) return;
  if (url.pathname.includes('google-analytics')) return;

  // HTML pages: Network-first strategy (always check for updates)
  if (url.pathname === '/' || url.pathname.endsWith('.html')) {
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          // Cache the fresh response
          const clonedResponse = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clonedResponse));
          return response;
        })
        .catch(() => {
          // Fallback to cache if network fails
          return caches.match(event.request).then((cached) => {
            return cached || caches.match('/index.html');
          });
        })
    );
    return;
  }

  // Static assets (images, CSS, JS): Cache-first for speed
  if (url.pathname.match(/\.(webp|png|jpg|jpeg|gif|svg|css|js|woff2|woff|ttf)$/)) {
    event.respondWith(
      caches.match(event.request)
        .then((cached) => {
          if (cached) return cached;
          return fetch(event.request).then((response) => {
            const clonedResponse = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clonedResponse));
            return response;
          });
        })
    );
    return;
  }

  // Default: Network only (for API and dynamic content)
  event.respondWith(fetch(event.request));
});

// ─── Background Sync (Offline Orders) ───
self.addEventListener('sync', (event) => {
  if (event.tag === 'order-sync') {
    event.waitUntil(syncOrders());
  }
});

async function syncOrders() {
  try {
    const cache = await caches.open('order-queue');
    const requests = await cache.keys();
    for (const request of requests) {
      const response = await cache.match(request);
      if (response) {
        const data = await response.json();
        // Replay the order to the server
        const replay = await fetch('/api/orders', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(data)
        });
        if (replay.ok) {
          await cache.delete(request);
          console.log('✅ Offline order synced:', data.payment_reference);
        }
      }
    }
  } catch (err) {
    console.error('❌ Sync failed:', err);
  }
}

// ─── Push Notifications ───
self.addEventListener('push', (event) => {
  const data = event.data.json();
  const options = {
    body: data.body || 'Your order has been updated!',
    icon: '/fastfood/icons/icon-192.png',
    badge: '/fastfood/icons/badge-icon.png',
    vibrate: [200, 100, 200],
    actions: [
      { action: 'view-order', title: 'View Order' },
      { action: 'dismiss', title: 'Dismiss' }
    ],
    data: {
      orderId: data.orderId || null,
      url: data.url || '/'
    }
  };
  event.waitUntil(
    self.registration.showNotification(data.title || 'Hot Portion Grill', options)
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  if (event.action === 'view-order' && event.notification.data.url) {
    event.waitUntil(
      clients.openWindow(event.notification.data.url)
    );
  } else {
    event.waitUntil(clients.openWindow('/'));
  }
});

console.log('✅ Hot Portion Service Worker active');

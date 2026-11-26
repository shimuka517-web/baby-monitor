// 育児ダッシュボードの Service Worker：ラズパイからのアプリ通知（Web Push）を表示する
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

self.addEventListener('push', (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = { body: e.data ? e.data.text() : '' }; }
  const alert = d.kind === 'alert';
  e.waitUntil(self.registration.showNotification(d.title || '育児の記録', {
    body: d.body || '',
    icon: '/icon-180.png',
    badge: '/icon-180.png',
    tag: d.tag || 'baby-monitor',
    renotify: true,                 // 同じ種類でも、新しい通知は毎回知らせる
    requireInteraction: alert,      // 見守りアラートは、触るまで消えない
    vibrate: alert ? [300, 150, 300, 150, 300] : [150],
    data: { url: d.url || '/dash' },
  }));
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = new URL(e.notification.data && e.notification.data.url || '/dash', self.location.origin).href;
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
    for (const c of list) {
      if (c.url.startsWith(self.location.origin)) { c.focus(); return c.navigate(url); }
    }
    return self.clients.openWindow(url);
  }));
});

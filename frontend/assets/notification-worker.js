// This worker displays notifications only. It does not cache requests or carry API credentials.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(
    (async () => {
      const target = new URL('/#runs', self.location.origin).href;
      const pages = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
      for (const page of pages) {
        if (
          new URL(page.url).origin === self.location.origin &&
          new URL(page.url).pathname === '/'
        ) {
          // Preserve unsaved edits in an already open Studio.
          return page.focus();
        }
      }
      return self.clients.openWindow(target);
    })()
  );
});

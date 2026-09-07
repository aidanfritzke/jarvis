// Jarvis service worker — receives Web Push and shows a notification.
// Served from / (root scope) so it can handle push for the whole app.

self.addEventListener('push', (event) => {
  let data = { title: 'Jarvis', body: '' };
  try {
    if (event.data) data = event.data.json();
  } catch (e) {
    if (event.data) data = { title: 'Jarvis', body: event.data.text() };
  }
  const title = data.title || 'Jarvis';
  const options = { body: data.body || '', icon: '/icon.png', badge: '/icon.png' };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
      for (const w of wins) { if ('focus' in w) return w.focus(); }
      if (clients.openWindow) return clients.openWindow('/');
    })
  );
});

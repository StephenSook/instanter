/* Instanter service worker. Push payload carries no case data. */

self.addEventListener("push", (event) => {
  let title = "Instanter";
  let body = "A sweep is waiting on an attorney.";
  try {
    const data = event.data ? event.data.json() : {};
    if (typeof data.title === "string") title = data.title;
    if (typeof data.body === "string") body = data.body;
  } catch {
    /* keep the defaults */
  }
  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      icon: "/favicon.svg",
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(self.clients.openWindow("/#sweep"));
});

/*
 * Service worker for Web Push (dashboard_api/web_push.py). Its only job is to
 * show what the supervisor pushed and, on click, bring the dashboard up on
 * the right workspace/conversation. No caching, no offline mode: the dashboard
 * is useless without the API anyway, and a cache is one more way to serve a
 * stale bundle after a deploy.
 *
 * Served from the site root so its scope covers the whole dashboard. Plain JS
 * on purpose: it is copied as-is from public/, not bundled.
 */

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = {};
  }
  const title = data.title || "Dashboard";
  // A push must always show something: browsers penalise (or warn about) a
  // site that receives one silently.
  event.waitUntil(
    self.registration.showNotification(title, {
      body: data.body || "",
      // Same tag replaces instead of stacking, and is shared with the
      // in-page notifications so the two never show the same thing twice.
      tag: data.tag || undefined,
      data: {
        url: data.url || "/",
        account_id: data.account_id || null,
        conversation_id: data.conversation_id || null,
      },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = event.notification.data || {};
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      const open = windows.find((client) => new URL(client.url).origin === self.location.origin);
      if (open) {
        // The page switches workspace/page itself (App.tsx listens for this).
        open.postMessage({ type: "push-open", ...target });
        return open.focus();
      }
      const url = new URL(target.url || "/", self.location.origin);
      if (target.account_id) url.searchParams.set("open_account", target.account_id);
      if (target.conversation_id) url.searchParams.set("open_conversation", target.conversation_id);
      return self.clients.openWindow(url.href);
    })(),
  );
});

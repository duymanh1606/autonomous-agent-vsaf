// Runs in the page's own JS world (manifest.json declares "world": "MAIN"),
// not the isolated content-script world every other file here runs in - this
// is the only way to see a call the page itself makes to `new Notification(...)`.
//
// Why this exists: TikTok's own "message request" queue (a stranger's first
// message) does not refresh while the page sits loaded - confirmed live, it
// only shows up after a reload. Chrome's own DOM (what every other file here
// reads) never changes when a native Notification fires, since it renders
// outside the page entirely, in the OS's notification surface - so this is
// the only in-page hook that can catch it at all, IF TikTok uses the
// Notification API for it. It is intentionally cheap and narrow: it does not
// suppress or alter the real notification (the page still gets a real
// Notification object back), it only reports that one was requested.
(() => {
  if (window.__agentNotificationHookInstalled) return;
  window.__agentNotificationHookInstalled = true;

  const OriginalNotification = window.Notification;
  if (typeof OriginalNotification !== "function") return;

  function PatchedNotification(title, options) {
    try {
      window.dispatchEvent(new CustomEvent("autonomous-agent-notification-seen", {
        detail: {
          title: String(title || "").slice(0, 200),
          body: String((options && options.body) || "").slice(0, 200),
          ts: Date.now()
        }
      }));
    } catch (_error) {
      // Never let a reporting failure break the page's own notification.
    }
    return new OriginalNotification(title, options);
  }
  PatchedNotification.prototype = OriginalNotification.prototype;
  PatchedNotification.permission = OriginalNotification.permission;
  PatchedNotification.requestPermission = OriginalNotification.requestPermission.bind(OriginalNotification);
  Object.defineProperty(window, "Notification", {
    value: PatchedNotification,
    writable: true,
    configurable: true
  });
})();

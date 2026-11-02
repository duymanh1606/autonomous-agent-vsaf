import type { AgentEvent } from "./api";
import { PUSHED_EVENT_TYPES, pushActive } from "./webPush";

/**
 * Desktop notifications for the few events someone has to act on, shown by
 * the browser itself (the Notification API) while a dashboard tab is open -
 * minimised or behind another tab included. The events already arrive over
 * the dashboard's WebSocket. Where the browser has Web Push (lib/webPush.ts),
 * the service worker shows the pushed event types instead - with the
 * dashboard closed too - and this file keeps only the rest.
 *
 * The browser only allows this in a secure context - HTTPS (the Cloudflare
 * Tunnel) or http://localhost / 127.0.0.1 (ssh -L). Over plain http://<LAN ip>
 * `Notification` is missing, and every function here says so rather than
 * failing.
 */

const PREFERENCE_KEY = "dashboard.browserNotifications";

/** Older than this when it arrives, and it is not news: the hub replays the
 *  latest `health.changed` to every browser that (re)connects, and a reload
 *  must not pop up a failure from ten minutes ago. */
const FRESH_MS = 60_000;

export type BrowserNotificationSupport = "ok" | "insecure" | "unsupported";

export function notificationSupport(): BrowserNotificationSupport {
  if (typeof window === "undefined") return "unsupported";
  if (!window.isSecureContext) return "insecure";
  return "Notification" in window ? "ok" : "unsupported";
}

export function notificationPermission(): NotificationPermission | null {
  return notificationSupport() === "ok" ? Notification.permission : null;
}

/** Per browser, not per account: it is this machine's screen that pops up.
 *  Storage can be unavailable (private mode, blocked site data) - then the
 *  answer is simply "off". */
export function notificationsEnabled(): boolean {
  try {
    return window.localStorage.getItem(PREFERENCE_KEY) === "on";
  } catch {
    return false;
  }
}

export function setNotificationsEnabled(enabled: boolean): void {
  try {
    window.localStorage.setItem(PREFERENCE_KEY, enabled ? "on" : "off");
  } catch {
    // Nothing to do: without storage the preference cannot outlive the page.
  }
}

/** Ask the browser for permission (it must come from a click) and remember
 *  the choice. Resolves to whether notifications will now be shown. */
export async function enableNotifications(): Promise<boolean> {
  if (notificationSupport() !== "ok") return false;
  const permission =
    Notification.permission === "default"
      ? await Notification.requestPermission()
      : Notification.permission;
  const granted = permission === "granted";
  setNotificationsEnabled(granted);
  return granted;
}

export type NotificationContent = { title: string; body: string; tag: string };

/** What to show for an event, or null for the ones not worth interrupting
 *  someone for. */
export function describeEvent(event: AgentEvent, workspace: string): NotificationContent | null {
  const payload = event.payload ?? {};
  const text = (key: string) => {
    const value = payload[key];
    return typeof value === "string" ? value.trim() : "";
  };
  const base = `${event.account_id}:${event.type}`;
  switch (event.type) {
    case "escalation.opened": {
      const who = text("conversation") || "Một khách hàng";
      const said = text("text");
      return {
        title: `Cần người xử lý - ${workspace}`,
        body: said ? `${who}: ${said}` : `${who} đang chờ nhân viên tiếp quản.`,
        // One per conversation: the same customer escalating twice replaces
        // the first popup instead of stacking a second.
        tag: `${base}:${text("conversation_id") || who}`,
      };
    }
    case "auth.required":
      return {
        title: `Mất phiên đăng nhập TikTok - ${workspace}`,
        body: "Workspace đã bị đăng xuất khỏi TikTok, cần đăng nhập lại.",
        tag: base,
      };
    case "auth.security_challenge":
      return {
        title: `TikTok yêu cầu xác minh - ${workspace}`,
        body: "Cần giải CAPTCHA/xác minh bảo mật bằng tay qua màn hình Chrome.",
        tag: base,
      };
    case "auth.rate_limited":
      return {
        title: `TikTok tạm giới hạn đăng nhập - ${workspace}`,
        body: "Đợi một lúc rồi thử lại.",
        tag: base,
      };
    case "workspace.degraded":
      return {
        title: `Workspace không tự đánh thức được - ${workspace}`,
        body: "Workspace đang ngủ và đánh thức thất bại nhiều lần. Kiểm tra và Start lại.",
        tag: base,
      };
    case "health.changed": {
      if (payload.ok !== false) return null;
      const component = text("component") || "Thành phần";
      return {
        title: `${component} mất kết nối - ${workspace}`,
        body: text("detail") || "Agent có thể đã ngừng trả lời khách.",
        tag: `${base}:${component}`,
      };
    }
    default:
      return null;
  }
}

export function isFresh(event: AgentEvent, now = Date.now()): boolean {
  const at = Date.parse(event.ts);
  return Number.isFinite(at) && now - at <= FRESH_MS;
}

/**
 * Show a desktop notification for `event` if it deserves one and the user
 * has switched them on. Only while the dashboard is not what they are
 * looking at - with it in front of them, the in-app alerts already say it.
 * Returns the Notification (for tests and click handling) or null.
 */
export function notifyForEvent(
  event: AgentEvent,
  workspace: string,
  onClick: () => void,
): Notification | null {
  if (!notificationsEnabled() || notificationPermission() !== "granted") return null;
  // The service worker already shows these on this browser.
  if (pushActive() && PUSHED_EVENT_TYPES.has(event.type)) return null;
  if (document.visibilityState === "visible" && document.hasFocus()) return null;
  if (!isFresh(event)) return null;
  const content = describeEvent(event, workspace);
  if (!content) return null;
  try {
    const notification = new Notification(content.title, {
      body: content.body,
      // Same tag replaces instead of stacking - which is also what keeps two
      // open dashboard tabs from each popping up the same event.
      tag: content.tag,
    });
    notification.onclick = () => {
      window.focus();
      onClick();
      notification.close();
    };
    return notification;
  } catch {
    // Some mobile browsers expose the constructor but refuse to use it
    // outside a service worker. Not worth breaking the event stream over.
    return null;
  }
}

/** A sample popup, so someone switching this on sees what it looks like and
 *  that their OS is not silencing it (Focus Assist, Do Not Disturb). */
export function sendTestNotification(): boolean {
  if (notificationPermission() !== "granted") return false;
  try {
    new Notification("Thông báo thử", {
      body: "Thông báo trình duyệt đang hoạt động.",
      tag: "dashboard:test",
    });
    return true;
  } catch {
    return false;
  }
}

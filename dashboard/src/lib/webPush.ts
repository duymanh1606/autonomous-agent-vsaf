import { api } from "./api";

/**
 * Web Push: notifications that arrive with the dashboard closed. The page only
 * subscribes this browser; the supervisor decides what to send and to whom
 * (dashboard_api/web_push.py) and public/sw.js shows it.
 *
 * Needs the same secure context as the in-page notifications plus a service
 * worker and PushManager. On iPhone/iPad that means the dashboard added to the
 * home screen - Safari in a normal tab has no PushManager. Where push is not
 * available, the in-page notifications (lib/browserNotifications.ts) are what
 * the user gets.
 */

const PREFERENCE_KEY = "dashboard.webPush";

/** What the supervisor pushes (dashboard_api/web_push.py `describe`). The
 *  in-page notifications still cover everything else, e.g. a lost agent. */
export const PUSHED_EVENT_TYPES: ReadonlySet<string> = new Set([
  "escalation.opened",
  "auth.required",
  "auth.security_challenge",
  "auth.rate_limited",
  "workspace.degraded",
]);
const WORKER_URL = "/sw.js";

export function pushSupported(): boolean {
  return (
    typeof window !== "undefined" &&
    window.isSecureContext &&
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window
  );
}

/** Whether this browser is meant to be subscribed. Per browser, like the
 *  in-page preference; unavailable storage reads as "off". */
export function pushEnabled(): boolean {
  try {
    return window.localStorage.getItem(PREFERENCE_KEY) === "on";
  } catch {
    return false;
  }
}

function setPushEnabled(enabled: boolean): void {
  try {
    window.localStorage.setItem(PREFERENCE_KEY, enabled ? "on" : "off");
  } catch {
    // Without storage the preference cannot outlive the page.
  }
}

function keyBytes(base64url: string): Uint8Array<ArrayBuffer> {
  const padded = base64url.replace(/-/g, "+").replace(/_/g, "/").padEnd(
    Math.ceil(base64url.length / 4) * 4, "=",
  );
  const raw = atob(padded);
  const bytes = new Uint8Array(new ArrayBuffer(raw.length));
  for (let index = 0; index < raw.length; index += 1) bytes[index] = raw.charCodeAt(index);
  return bytes;
}

function sameKey(current: ArrayBuffer | null | undefined, wanted: Uint8Array): boolean {
  if (!current) return false;
  const bytes = new Uint8Array(current);
  return bytes.length === wanted.length && bytes.every((value, index) => value === wanted[index]);
}

async function registration(): Promise<ServiceWorkerRegistration> {
  await navigator.serviceWorker.register(WORKER_URL);
  return navigator.serviceWorker.ready;
}

/** Subscribe this browser and tell the supervisor. Permission must already be
 *  granted (lib/browserNotifications.ts asks for it from the click). Also
 *  repairs a subscription made for a key the server no longer has. */
async function subscribe(): Promise<void> {
  const [{ public_key: publicKey }, worker] = await Promise.all([api.pushConfig(), registration()]);
  const wanted = keyBytes(publicKey);
  let subscription = await worker.pushManager.getSubscription();
  if (subscription && !sameKey(subscription.options.applicationServerKey, wanted)) {
    await subscription.unsubscribe();
    subscription = null;
  }
  if (!subscription) {
    subscription = await worker.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: wanted,
    });
  }
  await api.pushSubscribe(subscription.toJSON());
}

/** Turn push on for this browser. Resolves to whether it worked. */
export async function enablePush(): Promise<boolean> {
  if (!pushSupported() || Notification.permission !== "granted") return false;
  try {
    await subscribe();
    setPushEnabled(true);
    return true;
  } catch {
    setPushEnabled(false);
    return false;
  }
}

export async function disablePush(): Promise<void> {
  setPushEnabled(false);
  if (!pushSupported()) return;
  try {
    const worker = await navigator.serviceWorker.getRegistration(WORKER_URL);
    const subscription = await worker?.pushManager.getSubscription();
    if (!subscription) return;
    const { endpoint } = subscription;
    await subscription.unsubscribe();
    await api.pushUnsubscribe(endpoint);
  } catch {
    // Already gone on one side; the server drops it at the next failed send.
  }
}

/** On every dashboard load: push subscriptions can be rotated or dropped by
 *  the browser, or the server can lose them; re-sending is idempotent. */
export async function syncPush(): Promise<void> {
  if (!pushEnabled() || !pushSupported()) return;
  if (Notification.permission !== "granted") {
    setPushEnabled(false);
    return;
  }
  try {
    await subscribe();
  } catch {
    // Next load tries again; the in-page notifications still work meanwhile.
  }
}

/** Push active here means the service worker shows every notification, so
 *  the page must not show its own copy as well. */
export function pushActive(): boolean {
  return pushSupported() && pushEnabled() && Notification.permission === "granted";
}

export type PushOpenTarget = { account_id: string | null; conversation_id: string | null };

/** Where a clicked notification asked to go: a message from the service
 *  worker (dashboard already open) or the query string it opened us with. */
export function onPushOpen(handler: (target: PushOpenTarget) => void): () => void {
  if (!("serviceWorker" in navigator)) return () => {};
  const listener = (event: MessageEvent) => {
    const data = event.data;
    if (!data || data.type !== "push-open") return;
    handler({ account_id: data.account_id ?? null, conversation_id: data.conversation_id ?? null });
  };
  navigator.serviceWorker.addEventListener("message", listener);
  return () => navigator.serviceWorker.removeEventListener("message", listener);
}

export function takeLaunchTarget(): PushOpenTarget | null {
  const params = new URLSearchParams(window.location.search);
  const account = params.get("open_account");
  if (!account) return null;
  const target = { account_id: account, conversation_id: params.get("open_conversation") };
  params.delete("open_account");
  params.delete("open_conversation");
  const query = params.toString();
  window.history.replaceState(null, "", window.location.pathname + (query ? `?${query}` : ""));
  return target;
}

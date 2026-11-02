import { notifyForEvent, setNotificationsEnabled } from "./browserNotifications";
import type { AgentEvent } from "./api";
import { pushActive, takeLaunchTarget } from "./webPush";

class FakeNotification {
  static permission: NotificationPermission = "granted";
  static shown: FakeNotification[] = [];
  onclick: (() => void) | null = null;
  close = vi.fn();
  constructor(public title: string) {
    FakeNotification.shown.push(this);
  }
}

const event = (type: string): AgentEvent =>
  ({ account_id: "shop-1", type, ts: new Date().toISOString(), payload: { conversation: "Lan" } });

describe("web push", () => {
  beforeEach(() => {
    FakeNotification.shown = [];
    vi.stubGlobal("Notification", FakeNotification);
    vi.stubGlobal("PushManager", class {});
    vi.stubGlobal("isSecureContext", true);
    Object.defineProperty(navigator, "serviceWorker", {
      configurable: true,
      value: { addEventListener: vi.fn(), removeEventListener: vi.fn() },
    });
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    vi.spyOn(document, "hasFocus").mockReturnValue(false);
    setNotificationsEnabled(true);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    window.localStorage.clear();
    delete (navigator as { serviceWorker?: unknown }).serviceWorker;
  });

  it("leaves pushed events to the service worker, and still shows the rest", () => {
    window.localStorage.setItem("dashboard.webPush", "on");
    expect(pushActive()).toBe(true);

    expect(notifyForEvent(event("escalation.opened"), "Shop A", () => {})).toBeNull();
    // Not pushed by the server: the page is still the only one to show it.
    const lost = { ...event("health.changed"), payload: { ok: false, component: "control" } };
    expect(notifyForEvent(lost, "Shop A", () => {})).not.toBeNull();
  });

  it("shows everything itself while push is off", () => {
    expect(pushActive()).toBe(false);
    expect(notifyForEvent(event("escalation.opened"), "Shop A", () => {})).not.toBeNull();
  });

  it("reads where a clicked push asked to go, then clears it from the address bar", () => {
    window.history.replaceState(null, "", "/?open_account=shop-1&open_conversation=c1&x=1");
    expect(takeLaunchTarget()).toEqual({ account_id: "shop-1", conversation_id: "c1" });
    expect(window.location.search).toBe("?x=1");
    expect(takeLaunchTarget()).toBeNull();
  });
});

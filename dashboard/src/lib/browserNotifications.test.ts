import {
  describeEvent,
  enableNotifications,
  notificationSupport,
  notifyForEvent,
  setNotificationsEnabled,
} from "./browserNotifications";
import type { AgentEvent } from "./api";

class FakeNotification {
  static permission: NotificationPermission = "granted";
  static requestPermission = vi.fn(async () => FakeNotification.permission);
  static shown: FakeNotification[] = [];
  onclick: (() => void) | null = null;
  close = vi.fn();
  constructor(public title: string, public options: NotificationOptions) {
    FakeNotification.shown.push(this);
  }
}

const event = (type: string, payload: Record<string, unknown> = {}, ts = new Date().toISOString()): AgentEvent =>
  ({ account_id: "shop-1", type, ts, payload });

function setVisible(visible: boolean) {
  vi.spyOn(document, "visibilityState", "get").mockReturnValue(visible ? "visible" : "hidden");
  vi.spyOn(document, "hasFocus").mockReturnValue(visible);
}

describe("browser notifications", () => {
  beforeEach(() => {
    FakeNotification.permission = "granted";
    FakeNotification.shown = [];
    FakeNotification.requestPermission.mockClear();
    vi.stubGlobal("Notification", FakeNotification);
    vi.stubGlobal("isSecureContext", true);
    setNotificationsEnabled(true);
    setVisible(false);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    window.localStorage.clear();
  });

  it("pops up an escalation with who is waiting and what they said", () => {
    const onClick = vi.fn();
    vi.spyOn(window, "focus").mockImplementation(() => {});
    const shown = notifyForEvent(
      event("escalation.opened", { conversation: "Lan", conversation_id: "c1", text: "size M còn không" }),
      "Shop A", onClick,
    ) as unknown as FakeNotification;

    expect(shown.title).toBe("Cần người xử lý - Shop A");
    expect(shown.options.body).toBe("Lan: size M còn không");
    shown.onclick?.();
    expect(onClick).toHaveBeenCalled();
  });

  it("stays quiet while the dashboard is what the user is looking at", () => {
    setVisible(true);
    expect(notifyForEvent(event("auth.required"), "Shop A", () => {})).toBeNull();
  });

  it("stays quiet when switched off, or for an old replayed event", () => {
    setNotificationsEnabled(false);
    expect(notifyForEvent(event("auth.required"), "Shop A", () => {})).toBeNull();

    setNotificationsEnabled(true);
    const tenMinutesAgo = new Date(Date.now() - 10 * 60_000).toISOString();
    expect(notifyForEvent(event("health.changed", { ok: false }, tenMinutesAgo), "Shop A", () => {})).toBeNull();
    expect(FakeNotification.shown).toHaveLength(0);
  });

  it("only notifies for events someone has to act on", () => {
    expect(describeEvent(event("conversation.message.received"), "A")).toBeNull();
    expect(describeEvent(event("health.changed", { ok: true }), "A")).toBeNull();
    expect(describeEvent(event("health.changed", { ok: false, component: "chrome" }), "A")?.title)
      .toBe("chrome mất kết nối - A");
    for (const type of ["auth.required", "auth.security_challenge", "auth.rate_limited", "workspace.degraded"]) {
      expect(describeEvent(event(type), "A")).not.toBeNull();
    }
  });

  it("is unavailable over plain HTTP, and says so instead of throwing", async () => {
    vi.stubGlobal("isSecureContext", false);
    expect(notificationSupport()).toBe("insecure");
    expect(await enableNotifications()).toBe(false);
    expect(notifyForEvent(event("auth.required"), "A", () => {})).toBeNull();
  });

  it("asks for permission once and remembers a refusal as off", async () => {
    setNotificationsEnabled(false);
    FakeNotification.permission = "default";
    FakeNotification.requestPermission.mockResolvedValueOnce("denied");
    expect(await enableNotifications()).toBe(false);
    expect(FakeNotification.requestPermission).toHaveBeenCalledTimes(1);
  });
});

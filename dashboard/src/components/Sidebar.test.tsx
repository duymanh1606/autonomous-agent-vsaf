import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Sidebar } from "./Sidebar";
import type { Account } from "../lib/api";

const base = {
  index: 0, cdp_port: 1, control_port: 1, config_path: "",
  profile_dir: "", profile_dir_name: "", has_api_key: true, owner_id: null,
  owner_email: null, database_path: "", database: { ok: true, detail: "" },
  pinned: false, lifecycle_state: "active", idle_seconds: 0,
} as unknown as Account;

const running: Account = {
  ...base, id: "ws-run", label: "shop@sh.com", control_connected: true,
  processes: { chrome: { running: true, pid: 1 }, agent: { running: true, pid: 2 } },
} as Account;

// The state a workspace lands in when its agent dies at boot - a missing
// GEMINI_API_KEY, most often - or right after creation, before a key exists.
const chromeOnly: Account = {
  ...base, id: "ws-chrome", label: "shop3@sh.com", control_connected: false,
  processes: { chrome: { running: true, pid: 3 }, agent: { running: false, pid: null } },
} as Account;

const stopped: Account = {
  ...base, id: "ws-idle", label: "shop2@sh.com", control_connected: false,
  processes: { chrome: { running: false, pid: null }, agent: { running: false, pid: null } },
} as Account;

function renderSidebar(overrides: Partial<Parameters<typeof Sidebar>[0]> = {}) {
  const props = {
    accounts: [running, stopped, chromeOnly], statuses: {}, selected: "ws-idle", connection: "open",
    onSelect: vi.fn(), onCreate: vi.fn(), onDelete: vi.fn(async () => {}),
    onStart: vi.fn(), onStop: vi.fn(), busyId: null, page: "home" as const,
    onNavigate: vi.fn(), alertCount: 0, isAdmin: false, ...overrides,
  };
  return { ...render(<Sidebar {...props} />), props };
}

describe("Sidebar", () => {
  it("carries the product name, not the old one", () => {
    renderSidebar();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Agent TIKTOK");
    expect(screen.queryByText(/NEXUS/)).not.toBeInTheDocument();
  });

  describe("Log", () => {
    it("is only in the menu for an admin", async () => {
      const { unmount } = renderSidebar({ isAdmin: false });
      expect(screen.queryByRole("button", { name: /^Nhật ký hệ thống$/ })).not.toBeInTheDocument();
      unmount();

      const { props } = renderSidebar({ isAdmin: true });
      await userEvent.click(screen.getByRole("button", { name: /^Nhật ký hệ thống$/ }));
      expect(props.onNavigate).toHaveBeenCalledWith("logs");
    });
  });

  describe("Start", () => {
    it("is offered for a stopped workspace and starts it in one click", async () => {
      const { props } = renderSidebar({ selected: "ws-idle" });
      await userEvent.click(screen.getByRole("button", { name: /Khởi động shop2@sh.com/ }));
      expect(props.onStart).toHaveBeenCalledWith("ws-idle");
    });

    it("is not offered for a running workspace", () => {
      renderSidebar({ selected: "ws-run" });
      expect(screen.queryByRole("button", { name: /Khởi động/ })).not.toBeInTheDocument();
    });

    it("says so while the start is in flight", () => {
      renderSidebar({ selected: "ws-idle", busyId: "ws-idle" });
      expect(screen.getByRole("button", { name: /Khởi động/ })).toBeDisabled();
      expect(screen.getByText("Đang khởi động...")).toBeInTheDocument();
    });
  });

  describe("Stop", () => {
    it("needs two clicks: the first only arms it", async () => {
      const { props } = renderSidebar({ selected: "ws-run" });
      const stop = screen.getByRole("button", { name: /Dừng shop@sh.com/ });
      await userEvent.click(stop);
      expect(props.onStop).not.toHaveBeenCalled();
      expect(screen.getByText("Bấm lần nữa để dừng")).toBeInTheDocument();

      await userEvent.click(screen.getByRole("button", { name: /Bấm lần nữa/ }));
      expect(props.onStop).toHaveBeenCalledWith("ws-run");
    });

    it("disarms on its own if the second click never comes", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      const { props } = renderSidebar({ selected: "ws-run" });
      await userEvent.click(screen.getByRole("button", { name: /Dừng shop@sh.com/ }));
      expect(screen.getByText("Bấm lần nữa để dừng")).toBeInTheDocument();

      act(() => { vi.advanceTimersByTime(4500); });

      expect(screen.getByText("Dừng workspace")).toBeInTheDocument();
      expect(props.onStop).not.toHaveBeenCalled();
      vi.useRealTimers();
    });

    it("disarms when the operator switches workspace", async () => {
      const { rerender, props } = renderSidebar({ selected: "ws-run" });
      await userEvent.click(screen.getByRole("button", { name: /Dừng shop@sh.com/ }));
      expect(screen.getByText("Bấm lần nữa để dừng")).toBeInTheDocument();

      rerender(<Sidebar {...props} selected="ws-idle" />);
      rerender(<Sidebar {...props} selected="ws-run" />);

      // A stale "confirm" must never be waiting on a different workspace.
      expect(screen.getByText("Dừng workspace")).toBeInTheDocument();
    });
  });

  describe("a Chrome whose agent is not running", () => {
    it("is labelled honestly and can be stopped", async () => {
      const { props } = renderSidebar({ selected: "ws-chrome" });
      expect(screen.getByText("Chrome đang mở, agent đã tắt")).toBeInTheDocument();

      // The whole bug: this button did not exist, so that Chrome could never
      // be closed from the UI.
      await userEvent.click(screen.getByRole("button", { name: /Dừng shop3@sh.com/ }));
      await userEvent.click(screen.getByRole("button", { name: /Bấm lần nữa/ }));
      expect(props.onStop).toHaveBeenCalledWith("ws-chrome");
    });

    it("can also be started, which boots the agent next to the existing Chrome", () => {
      renderSidebar({ selected: "ws-chrome" });
      expect(screen.getByRole("button", { name: /Khởi động shop3@sh.com/ })).toBeInTheDocument();
    });
  });

  describe("workspace switcher", () => {
    it("is a proper listbox that reports which workspace is current", async () => {
      renderSidebar({ selected: "ws-idle" });
      const trigger = screen.getByRole("button", { name: /Chọn workspace khác/ });
      expect(trigger).toHaveAttribute("aria-expanded", "false");

      await userEvent.click(trigger);

      expect(trigger).toHaveAttribute("aria-expanded", "true");
      expect(screen.getByRole("listbox")).toBeInTheDocument();
      expect(screen.getByRole("option", { name: /shop2@sh.com/ })).toHaveAttribute("aria-selected", "true");
      expect(screen.getByRole("option", { name: /shop@sh.com/ })).toHaveAttribute("aria-selected", "false");
    });

    it("selects and closes on choosing an option, and closes on Escape", async () => {
      const { props } = renderSidebar({ selected: "ws-idle" });
      await userEvent.click(screen.getByRole("button", { name: /Chọn workspace khác/ }));
      await userEvent.click(screen.getByRole("option", { name: /shop@sh.com/ }));
      expect(props.onSelect).toHaveBeenCalledWith("ws-run");
      expect(screen.queryByRole("listbox")).not.toBeInTheDocument();

      await userEvent.click(screen.getByRole("button", { name: /Chọn workspace khác/ }));
      await userEvent.keyboard("{Escape}");
      expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    });
  });
});

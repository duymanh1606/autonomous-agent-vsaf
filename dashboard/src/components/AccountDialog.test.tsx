import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Topbar } from "./Topbar";
import { api } from "../lib/api";
import type { User } from "../lib/api";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, api: { ...actual.api, changePassword: vi.fn() } };
});

const staff: User = {
  id: 7, email: "nhanvien01", is_admin: false, account_ids: ["shop-a", "shop-b"], max_workspaces: 1,
};

function setup(user: User = staff) {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { mutations: { retry: false } } })}>
      <Topbar
        connection="open" notificationCount={0} onOpenAlerts={() => {}} onOpenSearch={() => {}}
        user={user} onLogout={() => {}}
      />
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

async function openAndFill(user: ReturnType<typeof userEvent.setup>, current: string, next: string, confirm = next) {
  await user.click(screen.getByRole("button", { name: "Tài khoản của tôi" }));
  await user.type(screen.getByLabelText("Mật khẩu hiện tại"), current);
  await user.type(screen.getByLabelText("Mật khẩu mới"), next);
  await user.type(screen.getByLabelText("Nhập lại mật khẩu mới"), confirm);
}

describe("Avatar - thông tin tài khoản + đổi mật khẩu", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows who is signed in when the avatar is clicked", async () => {
    const user = setup();
    expect(screen.getByRole("button", { name: "Tài khoản của tôi" }).textContent).toContain("N");

    await user.click(screen.getByRole("button", { name: "Tài khoản của tôi" }));

    const dialog = screen.getByRole("dialog", { name: "Tài khoản của tôi" });
    expect(dialog.textContent).toContain("nhanvien01");
    expect(dialog.textContent).toContain("Nhân viên");
    expect(dialog.textContent).toContain("Workspace được truy cập2");
  });

  it("changes the password and says the other devices were signed out", async () => {
    (api.changePassword as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true });
    const user = setup();
    await openAndFill(user, "mat khau cu", "mat khau moi");
    await user.click(screen.getByRole("button", { name: "Đổi mật khẩu" }));

    expect(api.changePassword).toHaveBeenCalledWith("mat khau cu", "mat khau moi");
    expect(await screen.findByText(/Đã đổi mật khẩu/)).toBeTruthy();
    expect((screen.getByLabelText("Mật khẩu hiện tại") as HTMLInputElement).value).toBe("");
  });

  it("will not send a mismatched confirmation or a too-short password", async () => {
    const user = setup();
    await openAndFill(user, "mat khau cu", "mat khau moi", "mat khau khac");
    expect(screen.getByText("Mật khẩu nhập lại không khớp.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Đổi mật khẩu" })).toBeDisabled();

    await user.clear(screen.getByLabelText("Mật khẩu mới"));
    await user.type(screen.getByLabelText("Mật khẩu mới"), "ngan");
    expect(screen.getByText("Mật khẩu mới cần tối thiểu 8 ký tự.")).toBeTruthy();
    expect(api.changePassword).not.toHaveBeenCalled();
  });

  it("shows the server's reason when the current password is wrong", async () => {
    (api.changePassword as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error("mật khẩu hiện tại không đúng"),
    );
    const user = setup();
    await openAndFill(user, "sai roi nhe", "mat khau moi");
    await user.click(screen.getByRole("button", { name: "Đổi mật khẩu" }));

    expect(await screen.findByText("mật khẩu hiện tại không đúng")).toBeTruthy();
  });
});

describe("Avatar - thông báo trình duyệt", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    window.localStorage.clear();
  });

  it("explains why it cannot work over plain HTTP", async () => {
    vi.stubGlobal("isSecureContext", false);
    const user = setup();
    await user.click(screen.getByRole("button", { name: "Tài khoản của tôi" }));
    expect(screen.getByText(/chỉ cho hiện thông báo khi dashboard được mở qua HTTPS/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Bật thông báo" })).toBeNull();
  });

  it("asks the browser, then offers a test and a way to switch it off", async () => {
    const requestPermission = vi.fn(async () => "granted" as NotificationPermission);
    const shown: string[] = [];
    vi.stubGlobal("isSecureContext", true);
    vi.stubGlobal("Notification", Object.assign(
      function (this: unknown, title: string) { shown.push(title); },
      { permission: "default", requestPermission },
    ));
    const user = setup();
    await user.click(screen.getByRole("button", { name: "Tài khoản của tôi" }));

    // The permission is read live, so flip it the way the browser would.
    requestPermission.mockImplementation(async () => {
      (window.Notification as unknown as { permission: string }).permission = "granted";
      return "granted";
    });
    await user.click(screen.getByRole("button", { name: "Bật thông báo" }));
    expect(requestPermission).toHaveBeenCalled();

    await user.click(await screen.findByRole("button", { name: "Gửi thử" }));
    expect(shown).toEqual(["Thông báo thử"]);

    await user.click(screen.getByRole("button", { name: "Tắt thông báo" }));
    expect(screen.getByRole("button", { name: "Bật thông báo" })).toBeTruthy();
  });
});

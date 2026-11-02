import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { UsersAdmin } from "./UsersAdmin";
import { api } from "../lib/api";
import type { Account, ManagedUser } from "../lib/api";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      users: vi.fn(),
      createUser: vi.fn(),
      grantAccess: vi.fn(),
      revokeAccess: vi.fn(),
      setUserMaxWorkspaces: vi.fn(),
      setUserPassword: vi.fn(),
      sendUserResetLink: vi.fn(),
      deactivateUser: vi.fn(),
      activateUser: vi.fn(),
      deleteUser: vi.fn(),
    },
  };
});

const proc = (running: boolean) =>
  ({ chrome: { running, pid: null }, agent: { running, pid: null } }) as Account["processes"];

const account = (id: string, label: string, ownerId: number | null, running = true): Account =>
  ({ id, label, owner_id: ownerId, processes: proc(running) } as unknown as Account);

const usage = { prompt_tokens: 4_000, completion_tokens: 1_000, total_tokens: 12_000, calls: 5 };

const CURRENT_USER_ID = 1;

// The signed-in admin - never shown as a row (see the "excludes the signed-
// in admin's own row" test below). `api.users()` still returns it, like the
// real backend does - the component itself does the filtering.
const self: ManagedUser = {
  id: CURRENT_USER_ID, email: "self-admin@shop.vn", is_admin: true, is_active: true,
  is_pending: false,
  created_at: "2026-08-01T00:00:00Z", account_ids: [], usage,
  owned_workspaces: 1, max_workspaces: null,
};
// A second, *other* admin - kept distinct from `self` so admin-role
// rendering is still exercised by a row that actually appears.
const otherAdmin: ManagedUser = {
  id: 4, email: "other-admin@shop.vn", is_admin: true, is_active: true, is_pending: false,
  created_at: "2026-08-05T00:00:00Z", account_ids: [], usage,
  owned_workspaces: 0, max_workspaces: null,
};
const member: ManagedUser = {
  id: 2, email: "nhanvien@shop.vn", is_admin: false, is_active: true, is_pending: false,
  // ws-2: owns it. ws-1: owned by self-admin, merely granted to this user -
  // the detail dialog is expected to show it under "Được cấp quyền", not
  // "Sở hữu", and the row's own workspace count (owner_id-based) excludes it.
  created_at: "2026-08-15T00:00:00Z", account_ids: ["ws-2", "ws-1"], usage,
  owned_workspaces: 2, max_workspaces: 3,
};
const disabled: ManagedUser = {
  id: 3, email: "cu@shop.vn", is_admin: false, is_active: false, is_pending: false,
  created_at: "2026-07-10T00:00:00Z", account_ids: [], usage: { ...usage, total_tokens: 0 },
  owned_workspaces: 0, max_workspaces: null,
};
// A self-registration nobody has looked at yet: inactive like `disabled`,
// but for the opposite reason - it is waiting on an admin, not switched off
// by one.
const pending: ManagedUser = {
  id: 5, email: "moi@shop.vn", is_admin: false, is_active: false, is_pending: true,
  created_at: "2026-09-20T00:00:00Z", account_ids: [], usage: { ...usage, total_tokens: 0 },
  owned_workspaces: 0, max_workspaces: null,
};

const accounts = [account("ws-1", "Shop Chính", CURRENT_USER_ID, true), account("ws-2", "Shop Phụ", 2, false)];

function setup(users: ManagedUser[] = [self, member, disabled], acc = accounts) {
  (api.users as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(users);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <UsersAdmin accounts={acc} currentUserId={CURRENT_USER_ID} />
    </QueryClientProvider>,
  );
}

function rowFor(email: string) {
  return screen.getByText(email).closest("tr")!;
}

describe("UsersAdmin", () => {
  beforeEach(() => vi.clearAllMocks());

  it("never shows the signed-in admin's own row, in the table or the summary/tab counts", async () => {
    setup([self, otherAdmin, member, disabled]);
    await screen.findByText("nhanvien@shop.vn"); // something real has to load first
    expect(screen.queryByText("self-admin@shop.vn")).not.toBeInTheDocument();
    // 3 of the 4 rows the "backend" returned are shown; self is excluded
    // everywhere, so numbers on screen agree with what is actually listed.
    expect(screen.getByText("Tất cả (3)")).toBeInTheDocument();
  });

  it("summarizes users and workspaces in the summary line from real data", async () => {
    setup([self, otherAdmin, member, disabled]);
    await screen.findByText("nhanvien@shop.vn");
    const summary = document.querySelector(".users-summary")!;
    // 3 visible users, 2 active, 2 total workspaces in `accounts`.
    expect(summary).toHaveTextContent("3");
    expect(summary).toHaveTextContent("người dùng");
    expect(summary).toHaveTextContent("2");
    expect(summary).toHaveTextContent("đang hoạt động");
    expect(summary).toHaveTextContent("workspace");
  });

  it("rows show identity, role, workspace count and status - table columns, not a card", async () => {
    setup();
    await screen.findByText("nhanvien@shop.vn");
    const row = rowFor("nhanvien@shop.vn");
    expect(row).toHaveTextContent("Member");
    expect(row).toHaveTextContent("1"); // owner_id-based: only ws-2, not the granted ws-1
    expect(row).toHaveTextContent("Hoạt động");
    expect(row).not.toHaveTextContent("Shop Phụ"); // no chips in the row itself
    expect(within(row).getByRole("button", { name: "Chi tiết" })).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Chỉnh sửa" })).toBeInTheDocument();
  });

  it("an admin row is labelled Admin", async () => {
    setup([self, otherAdmin, member, disabled]);
    await screen.findByText("other-admin@shop.vn");
    expect(rowFor("other-admin@shop.vn")).toHaveTextContent("Admin");
  });

  it("filters the table with the status tabs using real counts", async () => {
    const user = userEvent.setup();
    setup();
    await screen.findByText("nhanvien@shop.vn");
    expect(screen.getByText("cu@shop.vn")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Đang hoạt động (1)" }));
    expect(screen.queryByText("cu@shop.vn")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Đã vô hiệu hoá (1)" }));
    expect(screen.getByText("cu@shop.vn")).toBeInTheDocument();
    expect(screen.queryByText("nhanvien@shop.vn")).not.toBeInTheDocument();
  });

  it("Chi tiết opens a dialog with usage, owned/granted workspaces and access controls", async () => {
    const user = userEvent.setup();
    setup();
    await screen.findByText("nhanvien@shop.vn");
    await user.click(within(rowFor("nhanvien@shop.vn")).getByRole("button", { name: "Chi tiết" }));

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("nhanvien@shop.vn")).toBeInTheDocument();
    expect(within(dialog).getByText("12.0K")).toBeInTheDocument(); // token usage
    expect(within(dialog).getByText("Shop Phụ")).toBeInTheDocument(); // owned chip
    expect(within(dialog).getByText("Shop Chính")).toBeInTheDocument(); // granted chip
  });

  it("granting access from the detail dialog invalidates and the dialog reflects it without closing", async () => {
    const user = userEvent.setup();
    (api.grantAccess as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ granted: true });
    const threeAccounts = [...accounts, account("ws-3", "Shop Ba", null, true)];
    setup([self, member, disabled], threeAccounts);
    await screen.findByText("nhanvien@shop.vn");
    await user.click(within(rowFor("nhanvien@shop.vn")).getByRole("button", { name: "Chi tiết" }));

    const dialog = screen.getByRole("dialog");
    await user.selectOptions(within(dialog).getByLabelText("Cấp quyền vào workspace"), "ws-3");
    await waitFor(() => expect(api.grantAccess).toHaveBeenCalledWith(2, "ws-3"));
  });

  it("Chỉnh sửa opens a dialog that can change the password without touching the table", async () => {
    const user = userEvent.setup();
    (api.setUserPassword as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true });
    setup();
    await screen.findByText("nhanvien@shop.vn");
    await user.click(within(rowFor("nhanvien@shop.vn")).getByRole("button", { name: "Chỉnh sửa" }));

    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByPlaceholderText(/Mật khẩu mới/), "longenoughpass");
    await user.click(within(dialog).getByRole("button", { name: "Lưu mật khẩu" }));
    await waitFor(() => expect(api.setUserPassword).toHaveBeenCalledWith(2, "longenoughpass"));
  });

  it("offers 'Kích hoạt' for a pending signup and 'Kích hoạt lại' for one that was switched off", async () => {
    const user = userEvent.setup();
    setup([self, disabled, pending]);
    await screen.findByText("moi@shop.vn");

    // Never been active - there is no earlier state to restore, so "lại"
    // would describe a history this account does not have.
    await user.click(within(rowFor("moi@shop.vn")).getByRole("button", { name: "Chỉnh sửa" }));
    let dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("button", { name: /Kích hoạt/ })).toHaveTextContent(
      /^Kích hoạt$/,
    );
    await user.click(within(dialog).getByRole("button", { name: "Đóng" }));

    // Was active, an admin turned it off: putting it back is a restore.
    await user.click(within(rowFor("cu@shop.vn")).getByRole("button", { name: "Chỉnh sửa" }));
    dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("button", { name: /Kích hoạt/ })).toHaveTextContent(
      "Kích hoạt lại",
    );
  });

  it("Chỉnh sửa on a member saves a workspace quota, prefilled from the current one", async () => {
    const user = userEvent.setup();
    (api.setUserMaxWorkspaces as unknown as ReturnType<typeof vi.fn>)
      .mockResolvedValue({ max_workspaces: 5 });
    setup();
    await screen.findByText("nhanvien@shop.vn");
    await user.click(within(rowFor("nhanvien@shop.vn")).getByRole("button", { name: "Chỉnh sửa" }));

    const dialog = screen.getByRole("dialog");
    const input = within(dialog).getByLabelText("Số workspace được tạo");
    // `member.max_workspaces` is 3 - the field starts from what is in force,
    // so an admin edits a number rather than retyping one from memory.
    expect(input).toHaveValue("3");

    await user.clear(input);
    await user.type(input, "5");
    await user.click(within(dialog).getByRole("button", { name: "Lưu" }));
    await waitFor(() => expect(api.setUserMaxWorkspaces).toHaveBeenCalledWith(2, 5));
  });

  it("shows an ungranted quota as 0, not as unlimited", async () => {
    const user = userEvent.setup();
    setup([self, disabled]);
    await screen.findByText("cu@shop.vn");
    // `disabled.max_workspaces` is null. Nothing in this page may render
    // that as "∞": it means the admin has granted nothing, and the backend
    // refuses accordingly.
    expect(within(rowFor("cu@shop.vn")).getByText("0 / 0")).toBeInTheDocument();

    await user.click(within(rowFor("cu@shop.vn")).getByRole("button", { name: "Chỉnh sửa" }));
    expect(within(screen.getByRole("dialog")).getByLabelText("Số workspace được tạo")).toHaveValue("0");
  });

  it("Chỉnh sửa on another admin has no workspace-limit editor - admins are never capped", async () => {
    const user = userEvent.setup();
    setup([self, otherAdmin, member, disabled]);
    await screen.findByText("other-admin@shop.vn");
    await user.click(within(rowFor("other-admin@shop.vn")).getByRole("button", { name: "Chỉnh sửa" }));

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).queryByText("Giới hạn workspace")).not.toBeInTheDocument();
    expect(within(dialog).getByPlaceholderText(/Mật khẩu mới/)).toBeInTheDocument();
    // Not self (a *different* admin), so account-status controls are offered.
    expect(within(dialog).getByRole("button", { name: "Xoá tài khoản" })).toBeInTheDocument();
  });

  it("deleting from the edit dialog closes it and removes the user", async () => {
    const user = userEvent.setup();
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    (api.deleteUser as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ deleted: true });
    setup();
    await screen.findByText("nhanvien@shop.vn");
    await user.click(within(rowFor("nhanvien@shop.vn")).getByRole("button", { name: "Chỉnh sửa" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Xoá tài khoản" }));

    await waitFor(() => expect(api.deleteUser).toHaveBeenCalledWith(2));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    confirmSpy.mockRestore();
  });

  it("opening the create-user form (bottom of the table) calls the admin-only endpoint, never public register", async () => {
    const user = userEvent.setup();
    (api.createUser as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ ...member, id: 9 });
    setup();
    await screen.findByText("nhanvien@shop.vn");

    await user.click(screen.getByRole("button", { name: /Thêm người dùng/ }));
    await user.type(screen.getByPlaceholderText("tenban@gmail.com"), "new.staff@gmail.com");
    await user.type(screen.getByPlaceholderText(/Mật khẩu \(tối thiểu/), "longenoughpass");
    await user.click(screen.getByRole("button", { name: "Tạo tài khoản" }));

    await waitFor(() => expect(api.createUser).toHaveBeenCalledWith("new.staff@gmail.com", "longenoughpass"));
  });

  it("exports a CSV client-side without any network request, and self is not in it", async () => {
    const user = userEvent.setup();
    const clicks: string[] = [];
    const realCreateObjectURL = URL.createObjectURL;
    URL.createObjectURL = vi.fn(() => "blob:mock");
    URL.revokeObjectURL = vi.fn();
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(function (this: HTMLAnchorElement) { clicks.push(this.download); });

    setup();
    await screen.findByText("nhanvien@shop.vn");
    await user.click(screen.getByRole("button", { name: "Xuất dữ liệu" }));

    expect(clicks).toHaveLength(1);
    expect(clicks[0]).toMatch(/^nguoi-dung-\d{4}-\d{2}-\d{2}\.csv$/);

    clickSpy.mockRestore();
    URL.createObjectURL = realCreateObjectURL;
  });

  it("separates a signup waiting for approval from a disabled account", async () => {
    setup([self, member, disabled, pending]);
    await screen.findByText("moi@shop.vn");

    // Both are inactive, but only one of them needs somebody to do something.
    expect(screen.getByText("Chờ duyệt (1)")).toBeTruthy();
    expect(screen.getByText("Đã vô hiệu hoá (1)")).toBeTruthy();
    expect(screen.getAllByText("Chờ duyệt").length).toBeGreaterThan(0);
  });

  it("filters down to the pending signups", async () => {
    const user = userEvent.setup();
    setup([self, member, disabled, pending]);
    await screen.findByText("moi@shop.vn");

    await user.click(screen.getByText("Chờ duyệt (1)"));

    expect(screen.queryByText("cu@shop.vn")).toBeNull();
    expect(screen.queryByText("nhanvien@shop.vn")).toBeNull();
    expect(screen.getByText("moi@shop.vn")).toBeTruthy();
  });
});

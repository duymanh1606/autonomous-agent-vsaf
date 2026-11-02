import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Auth } from "./Auth";
import { api } from "../lib/api";

/**
 * Registering used to sign you in. On a dashboard that is only reachable from
 * the office that was merely generous; on a public URL it meant a stranger
 * held a logged-in session on their first ever request, and could go straight
 * on to create workspaces - each of which starts a Chrome and an agent
 * process on the server.
 *
 * The backend now creates the account inactive and returns a receipt instead
 * of a user. This screen has to say so, and must not navigate into the app.
 */
vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, register: vi.fn(), login: vi.fn(), forgotPassword: vi.fn() },
  };
});

function setup() {
  const onSuccess = vi.fn();
  render(
    <QueryClientProvider client={new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    })}>
      <Auth onSuccess={onSuccess} />
    </QueryClientProvider>,
  );
  return { onSuccess };
}

async function submitRegistration(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByText("Chưa có tài khoản? Đăng ký"));
  await user.type(screen.getByPlaceholderText("tenban@gmail.com"), "nhanvien01@gmail.com");
  await user.type(screen.getByPlaceholderText("Tối thiểu 8 ký tự"), "mat khau dai");
  await user.click(screen.getByRole("button", { name: "Tạo tài khoản" }));
}

describe("Auth", () => {
  beforeEach(() => vi.clearAllMocks());

  it("tells a new signup it is waiting for an admin, and stays put", async () => {
    const user = userEvent.setup();
    (api.register as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true, pending_approval: true,
    });
    const { onSuccess } = setup();

    await submitRegistration(user);

    expect(await screen.findByText(/cần quản trị viên phê duyệt/i)).toBeTruthy();
    // The thing that must not happen: being let into the app.
    expect(onSuccess).not.toHaveBeenCalled();
  });

  it("reports a rejected signup instead of the pending message", async () => {
    const user = userEvent.setup();
    (api.register as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error("email không hợp lệ"),
    );
    const { onSuccess } = setup();

    await submitRegistration(user);

    expect(await screen.findByText("email không hợp lệ")).toBeTruthy();
    expect(screen.queryByText(/cần quản trị viên phê duyệt/i)).toBeNull();
    expect(onSuccess).not.toHaveBeenCalled();
  });

  it("shows a taken email as a red banner, not as 'waiting for an admin'", async () => {
    const user = userEvent.setup();
    // What request() throws for a 409: "<status>: <body json>". errorMessage()
    // has to dig `detail` back out of that, or the person reads raw JSON.
    (api.register as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error('409: {"detail":"tên tài khoản đã được đăng ký"}'),
    );
    setup();

    await submitRegistration(user);

    const banner = await screen.findByText("tên tài khoản đã được đăng ký");
    expect(banner.className).toContain("bad");
    expect(screen.queryByText(/cần quản trị viên phê duyệt/i)).toBeNull();
  });

  it("opens the signup form empty, not with what was typed on login", async () => {
    const user = userEvent.setup();
    setup();

    await user.type(screen.getByPlaceholderText("tenban@gmail.com"), "abcxyz");
    await user.type(screen.getByPlaceholderText("Tối thiểu 8 ký tự"), "mat khau dai");
    await user.click(screen.getByText("Chưa có tài khoản? Đăng ký"));

    expect((screen.getByPlaceholderText("tenban@gmail.com") as HTMLInputElement).value).toBe("");
    expect((screen.getByPlaceholderText("Tối thiểu 8 ký tự") as HTMLInputElement).value).toBe("");
  });

  it("asks for a Gmail address when signing up, before sending anything", async () => {
    const user = userEvent.setup();
    setup();
    await user.click(screen.getByText("Chưa có tài khoản? Đăng ký"));
    await user.type(screen.getByPlaceholderText("tenban@gmail.com"), "nhanvien@shop.vn");
    await user.type(screen.getByPlaceholderText("Tối thiểu 8 ký tự"), "mat khau dai");

    expect(screen.getByText(/phải là địa chỉ Gmail/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Tạo tài khoản" })).toBeDisabled();
    expect(api.register).not.toHaveBeenCalled();
  });

  it("logs in with any address - accounts made before the Gmail rule still work", async () => {
    const user = userEvent.setup();
    setup();
    await user.type(screen.getByPlaceholderText("tenban@gmail.com"), "admin@shop.vn");
    expect(screen.queryByText(/Gmail/)).toBeNull();
    await user.clear(screen.getByPlaceholderText("tenban@gmail.com"));
    await user.type(screen.getByPlaceholderText("tenban@gmail.com"), "khong-phai-email");
    expect(screen.getByText("Email không hợp lệ.")).toBeTruthy();
  });

  it("tells a forgotten-password request to check the mailbox", async () => {
    const user = userEvent.setup();
    (api.forgotPassword as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true });
    setup();
    await user.click(screen.getByText("Quên mật khẩu?"));
    await user.type(screen.getByPlaceholderText("tenban@gmail.com"), "nhanvien01@gmail.com");
    await user.click(screen.getByRole("button", { name: "Gửi mật khẩu tạm" }));

    expect(api.forgotPassword).toHaveBeenCalledWith("nhanvien01@gmail.com");
    expect(await screen.findByText(/mật khẩu tạm thời đã được gửi tới hộp thư/)).toBeTruthy();
  });

  it("still signs in on a successful login", async () => {
    const user = userEvent.setup();
    const account = { id: 1, email: "op@shop.vn", is_admin: false, account_ids: [] };
    (api.login as ReturnType<typeof vi.fn>).mockResolvedValue(account);
    const { onSuccess } = setup();

    await user.type(screen.getByPlaceholderText("tenban@gmail.com"), "op@shop.vn");
    await user.type(screen.getByPlaceholderText("Tối thiểu 8 ký tự"), "mat khau dai");
    await user.click(screen.getByRole("button", { name: "Đăng nhập" }));

    // react-query hands its own extra arguments to onSuccess; only the
    // first one is this component's contract.
    await vi.waitFor(() => expect(onSuccess).toHaveBeenCalled());
    expect((onSuccess as ReturnType<typeof vi.fn>).mock.calls[0][0]).toEqual(account);
  });
});

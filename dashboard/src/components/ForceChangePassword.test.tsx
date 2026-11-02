import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ForceChangePassword } from "./ForceChangePassword";
import { api } from "../lib/api";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, api: { ...actual.api, changePassword: vi.fn() } };
});

it("after a temporary-password login, sets the new password and lets the user in", async () => {
  (api.changePassword as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true });
  const onChanged = vi.fn();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <ForceChangePassword
        user={{ id: 1, email: "nv@gmail.com", is_admin: false, account_ids: [], max_workspaces: 0, must_change_password: true }}
        onChanged={onChanged}
        onLogout={() => {}}
      />
    </QueryClientProvider>,
  );
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Mật khẩu tạm thời (trong email)"), "TamThoi123abc");
  await user.type(screen.getByLabelText("Mật khẩu mới"), "mat khau moi");
  await user.type(screen.getByLabelText("Nhập lại mật khẩu mới"), "mat khau moi");
  await user.click(screen.getByRole("button", { name: "Lưu mật khẩu mới" }));

  expect(api.changePassword).toHaveBeenCalledWith("TamThoi123abc", "mat khau moi");
  await vi.waitFor(() => expect(onChanged).toHaveBeenCalled());
});

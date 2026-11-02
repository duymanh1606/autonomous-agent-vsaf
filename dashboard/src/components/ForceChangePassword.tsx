import { ChangePasswordForm } from "./AccountDialog";
import type { User } from "../lib/api";

/**
 * Shown instead of the dashboard after a login with a mailed temporary
 * password. The server refuses every other API call until a new password is
 * set (dashboard_api/auth.py:MUST_CHANGE_PASSWORD_PATHS), so this screen is
 * the only thing that could work anyway - it just says so.
 */
export function ForceChangePassword({
  user,
  onChanged,
  onLogout,
}: {
  user: User;
  onChanged: () => void;
  onLogout: () => void;
}) {
  return (
    <div className="auth-screen">
      <div className="auth-card">
        <h1>Trung tâm điều khiển</h1>
        <h2>Đặt mật khẩu mới</h2>
        <p className="account-dialog-note">
          Bạn vừa đăng nhập bằng mật khẩu tạm thời gửi tới <strong>{user.email}</strong>.
          Hãy đặt mật khẩu mới để tiếp tục.
        </p>
        <ChangePasswordForm
          onChanged={onChanged}
          currentLabel="Mật khẩu tạm thời (trong email)"
          submitLabel="Lưu mật khẩu mới"
        />
        <div className="auth-links">
          <button type="button" onClick={onLogout}>Đăng xuất</button>
        </div>
      </div>
    </div>
  );
}

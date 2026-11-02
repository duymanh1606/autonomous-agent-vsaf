import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Modal } from "./Modal";
import { api } from "../lib/api";
import type { User } from "../lib/api";
import {
  enableNotifications,
  notificationPermission,
  notificationSupport,
  notificationsEnabled,
  sendTestNotification,
  setNotificationsEnabled,
} from "../lib/browserNotifications";
import { disablePush, enablePush, pushActive, pushSupported } from "../lib/webPush";

/** First character of the login name, for the round avatar in the topbar. */
export function avatarInitial(user: User): string {
  return (user.email.trim()[0] ?? "?").toUpperCase();
}

/**
 * "Tài khoản của tôi": who you are signed in as, and changing your own
 * password. The server re-checks every rule below (dashboard_api/main.py:
 * change_password); checking here too only saves a round trip for mistakes
 * that need no server to spot.
 */
export function AccountDialog({ user, onClose }: { user: User; onClose: () => void }) {
  const [done, setDone] = useState(false);
  const quota = user.is_admin ? "Không giới hạn" : String(user.max_workspaces ?? 0);

  return (
    <Modal onClose={onClose} title="Tài khoản của tôi" className="product-form-modal account-dialog">
      <div className="product-form-header">
        <h3>Tài khoản của tôi</h3>
        <button onClick={onClose}>Đóng</button>
      </div>

      <div className="account-dialog-identity">
        <span className="topbar-avatar-circle account-dialog-avatar" aria-hidden="true">
          {avatarInitial(user)}
        </span>
        <div>
          <div className="account-dialog-name">{user.email}</div>
          <div className="account-dialog-role">{user.is_admin ? "Quản trị viên" : "Nhân viên"}</div>
        </div>
      </div>

      <dl className="account-dialog-facts">
        <dt>Tên tài khoản</dt>
        <dd>{user.email}</dd>
        <dt>Vai trò</dt>
        <dd>{user.is_admin ? "Quản trị viên" : "Nhân viên"}</dd>
        <dt>Workspace được truy cập</dt>
        <dd>{user.is_admin ? "Tất cả" : user.account_ids.length}</dd>
        <dt>Hạn mức tạo workspace</dt>
        <dd>{quota}</dd>
      </dl>

      <BrowserNotificationSettings />

      <div className="customer-info-section-title">Đổi mật khẩu</div>
      {done && (
        <div className="banner" role="status">
          Đã đổi mật khẩu. Các thiết bị khác đang đăng nhập tài khoản này đã bị đăng xuất.
        </div>
      )}
      <ChangePasswordForm onChanged={() => setDone(true)} onEdit={() => setDone(false)} />
    </Modal>
  );
}

/**
 * Current password + new one twice. Used from "Tài khoản của tôi" and from
 * the screen that follows a login with a mailed temporary password (where
 * "current" is that temporary password).
 */
export function ChangePasswordForm({
  onChanged,
  onEdit,
  currentLabel = "Mật khẩu hiện tại",
  submitLabel = "Đổi mật khẩu",
}: {
  onChanged: () => void;
  onEdit?: () => void;
  currentLabel?: string;
  submitLabel?: string;
}) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");

  const change = useMutation({
    mutationFn: () => api.changePassword(current, next),
    onSuccess: () => {
      setCurrent("");
      setNext("");
      setConfirm("");
      onChanged();
    },
  });

  const problem =
    next && next.length < 8 ? "Mật khẩu mới cần tối thiểu 8 ký tự."
    : confirm && next !== confirm ? "Mật khẩu nhập lại không khớp."
    : next && current && next === current ? "Mật khẩu mới phải khác mật khẩu hiện tại."
    : null;
  const canSubmit = Boolean(current && next && confirm) && !problem && !change.isPending;
  const edit = (set: (value: string) => void) => (event: { target: { value: string } }) => {
    set(event.target.value);
    change.reset();
    onEdit?.();
  };

  return (
    <>
      {(problem || change.isError) && (
        <div className="banner bad" role="alert">
          {problem ?? (change.error instanceof Error ? change.error.message : "Đổi mật khẩu thất bại.")}
        </div>
      )}
      <form
        className="account-dialog-form"
        onSubmit={(event) => {
          event.preventDefault();
          if (canSubmit) change.mutate();
        }}
      >
        <label className="product-form-field">
          <span>{currentLabel}</span>
          <input type="password" autoComplete="current-password" value={current} onChange={edit(setCurrent)} />
        </label>
        <label className="product-form-field">
          <span>Mật khẩu mới</span>
          <input
            type="password"
            autoComplete="new-password"
            placeholder="Tối thiểu 8 ký tự"
            value={next}
            onChange={edit(setNext)}
          />
        </label>
        <label className="product-form-field">
          <span>Nhập lại mật khẩu mới</span>
          <input type="password" autoComplete="new-password" value={confirm} onChange={edit(setConfirm)} />
        </label>
        <button className="primary" type="submit" disabled={!canSubmit}>
          {change.isPending ? "Đang đổi..." : submitLabel}
        </button>
      </form>
    </>
  );
}

function BrowserNotificationSettings() {
  const support = notificationSupport();
  // Re-read after every click: permission and the stored preference both
  // live outside React.
  const [, rerender] = useState(0);
  const [testFailed, setTestFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [pushFailed, setPushFailed] = useState(false);
  const permission = notificationPermission();
  const on = notificationsEnabled() && permission === "granted";
  const push = on && pushActive();

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    try {
      await action();
    } finally {
      setBusy(false);
      rerender((n) => n + 1);
    }
  };
  const turnOn = () => run(async () => {
    setTestFailed(false);
    if (!(await enableNotifications())) return;
    // Push where the browser has it; the in-page notifications otherwise.
    setPushFailed(pushSupported() && !(await enablePush()));
  });
  const turnOnPush = () => run(async () => {
    setPushFailed(!(await enablePush()));
  });
  const turnOff = () => run(async () => {
    setNotificationsEnabled(false);
    setPushFailed(false);
    await disablePush();
  });
  const test = () => run(async () => {
    if (!push) {
      setTestFailed(!sendTestNotification());
      return;
    }
    // Through the server and the push service: proves the whole chain, not
    // just that this tab can draw a popup.
    try {
      setTestFailed((await api.pushTest()).delivered === 0);
    } catch {
      setTestFailed(true);
    }
  });

  let status: string;
  if (push) {
    status = "Đang bật thông báo đẩy trên trình duyệt này: nhận được cả khi đã đóng dashboard, miễn là trình duyệt còn chạy.";
  } else if (on && pushSupported()) {
    status = "Đang bật, nhưng chỉ khi dashboard đang mở (tab khác, cửa sổ khác, thu nhỏ). Bật thông báo đẩy để nhận cả khi đã đóng dashboard.";
  } else if (on) {
    status = "Đang bật trên trình duyệt này, chỉ khi dashboard đang mở. Trình duyệt này không hỗ trợ thông báo đẩy (iPhone/iPad: thêm dashboard vào Màn hình chính rồi mở từ đó).";
  } else {
    status = "Hiện thông báo khi có khách chờ tiếp quản, workspace mất đăng nhập TikTok hoặc bị lỗi. Trình duyệt hỗ trợ thông báo đẩy thì nhận được cả khi đã đóng dashboard.";
  }

  let body;
  if (support === "insecure") {
    body = (
      <p className="account-dialog-note">
        Trình duyệt chỉ cho hiện thông báo khi dashboard được mở qua HTTPS hoặc
        localhost. Địa chỉ đang dùng ({window.location.origin}) không thuộc hai loại
        đó - hỏi quản trị viên đường dẫn HTTPS.
      </p>
    );
  } else if (support === "unsupported") {
    body = <p className="account-dialog-note">Trình duyệt này không hỗ trợ thông báo.</p>;
  } else if (permission === "denied") {
    body = (
      <p className="account-dialog-note">
        Thông báo đang bị chặn cho trang này. Bấm biểu tượng ổ khoá cạnh thanh địa
        chỉ, cho phép "Thông báo", rồi tải lại trang.
      </p>
    );
  } else {
    body = (
      <>
        <p className="account-dialog-note">{status}</p>
        <div className="account-dialog-actions">
          {on ? (
            <>
              {!push && pushSupported() && (
                <button type="button" className="primary" disabled={busy} onClick={turnOnPush}>
                  Bật thông báo đẩy
                </button>
              )}
              <button type="button" disabled={busy} onClick={test}>
                Gửi thử
              </button>
              <button type="button" disabled={busy} onClick={turnOff}>
                Tắt thông báo
              </button>
            </>
          ) : (
            <button type="button" className="primary" disabled={busy} onClick={turnOn}>
              Bật thông báo
            </button>
          )}
        </div>
        {pushFailed && (
          <p className="account-dialog-note">
            Không đăng ký được thông báo đẩy (máy chủ hoặc dịch vụ push của trình duyệt không
            phản hồi). Thông báo vẫn hiện khi dashboard đang mở; thử lại sau.
          </p>
        )}
        {testFailed && (
          <p className="account-dialog-note">Không gửi được thông báo thử trên trình duyệt này.</p>
        )}
      </>
    );
  }

  return (
    <section className="account-dialog-section">
      <div className="customer-info-section-title">Thông báo trên trình duyệt</div>
      {body}
    </section>
  );
}

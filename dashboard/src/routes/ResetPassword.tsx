import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../lib/api";

export function ResetPassword({ token }: { token: string | null }) {
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const reset = useMutation({
    mutationFn: () => api.resetPassword(token ?? "", password),
    onSuccess: () => setDone(true),
    onError: (err: Error) => setError(err.message),
  });

  const canSubmit = password.length >= 8 && password === confirm;

  return (
    <div className="auth-screen">
      <div className="auth-card">
        <h1>Trung tâm điều khiển</h1>
        <h2>Đặt lại mật khẩu</h2>

        {!token ? (
          <div className="banner bad">Link đặt lại mật khẩu không hợp lệ - thiếu token.</div>
        ) : done ? (
          <>
            <div className="banner">Đã đổi mật khẩu thành công.</div>
            <button className="primary auth-submit" onClick={() => (window.location.href = "/")}>
              Về trang đăng nhập
            </button>
          </>
        ) : (
          <>
            {error && <div className="banner bad">{error}</div>}
            <form
              onSubmit={(event) => {
                event.preventDefault();
                if (canSubmit && !reset.isPending) reset.mutate();
              }}
            >
              <label className="product-form-field">
                <span>Mật khẩu mới</span>
                <input
                  type="password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  placeholder="Tối thiểu 8 ký tự"
                  autoFocus
                />
              </label>
              <label className="product-form-field">
                <span>Nhập lại mật khẩu mới</span>
                <input
                  type="password"
                  value={confirm}
                  onChange={(event) => setConfirm(event.target.value)}
                />
              </label>
              {confirm.length > 0 && password !== confirm && (
                <div className="banner bad">Hai mật khẩu chưa khớp.</div>
              )}
              <button
                className="primary auth-submit"
                type="submit"
                disabled={!canSubmit || reset.isPending}
              >
                Đặt lại mật khẩu
              </button>
            </form>
          </>
        )}
      </div>
    </div>
  );
}

import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../lib/api";
import type { User } from "../lib/api";
import { emailProblem, gmailProblem } from "../lib/email";

type Mode = "login" | "register" | "forgot";

const TITLE: Record<Mode, string> = {
  login: "Đăng nhập",
  register: "Đăng ký tài khoản",
  forgot: "Quên mật khẩu",
};

export function Auth({ onSuccess }: { onSuccess: (user: User) => void }) {
  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState(false);

  const login = useMutation({
    mutationFn: () => api.login(email.trim(), password),
    onSuccess,
    onError: (err: Error) => setError(errorMessage(err)),
  });

  const register = useMutation({
    mutationFn: () => api.register(email.trim(), password),
    // Not `onSuccess` (which signs the user in): a new account is inactive
    // until an admin approves it, so the form stays on the login screen and
    // says so.
    onSuccess: () => {
      setSent(true);
      setPassword("");
    },
    onError: (err: Error) => setError(errorMessage(err)),
  });

  const forgot = useMutation({
    mutationFn: () => api.forgotPassword(email.trim()),
    onSuccess: () => setSent(true),
    onError: (err: Error) => setError(errorMessage(err)),
  });

  const switchMode = (next: Mode) => {
    setMode(next);
    setError(null);
    setSent(false);
    // Every screen starts empty: carrying what was typed on one form over to
    // the next is surprising (and a login email showing up on the signup
    // form looks like the app remembered someone else's account).
    setEmail("");
    setPassword("");
  };

  const submit = () => {
    setError(null);
    if (mode === "login") login.mutate();
    else if (mode === "register") register.mutate();
    else forgot.mutate();
  };

  const pending = login.isPending || register.isPending || forgot.isPending;
  // A new account has to be Gmail; logging in and recovering accept any
  // address, so accounts made before that rule still work.
  const emailIssue = mode === "register" ? gmailProblem(email) : emailProblem(email);
  const canSubmit =
    email.trim().length > 0 && !emailIssue && (mode === "forgot" || password.length >= 8);

  return (
    <div className="auth-screen">
      <div className="auth-card">
        <h1>Trung tâm điều khiển</h1>
        <h2>{TITLE[mode]}</h2>

        {error && <div className="banner bad">{error}</div>}
        {mode === "register" && sent && (
          <div className="banner">
            Đã gửi yêu cầu đăng ký. Tài khoản cần quản trị viên phê duyệt trước khi
            đăng nhập được - liên hệ quản trị viên để được kích hoạt.
          </div>
        )}
        {mode === "forgot" && sent && (
          <div className="banner">
            Nếu email này có tài khoản, một mật khẩu tạm thời đã được gửi tới hộp thư
            (kiểm tra cả thư rác). Mật khẩu tạm dùng được trong 30 phút; đăng nhập
            bằng nó xong bạn sẽ được yêu cầu đặt mật khẩu mới. Mật khẩu cũ vẫn dùng
            được nếu bạn nhớ ra.
          </div>
        )}

        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (canSubmit && !pending) submit();
          }}
        >
          <label className="product-form-field">
            <span>Email</span>
            <input
              type="email"
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              value={email}
              aria-invalid={Boolean(emailIssue)}
              onChange={(event) => setEmail(event.target.value)}
              placeholder="tenban@gmail.com"
              autoFocus
            />
            {emailIssue && (
              <small className="product-form-error" role="alert">{emailIssue}</small>
            )}
          </label>

          {mode !== "forgot" && (
            <label className="product-form-field">
              <span>Mật khẩu</span>
              <input
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                placeholder="Tối thiểu 8 ký tự"
              />
            </label>
          )}

          <button className="primary auth-submit" type="submit" disabled={!canSubmit || pending}>
            {mode === "login" && "Đăng nhập"}
            {mode === "register" && "Tạo tài khoản"}
            {mode === "forgot" && "Gửi mật khẩu tạm"}
          </button>
        </form>

        <div className="auth-links">
          {mode === "login" && (
            <>
              <button onClick={() => switchMode("forgot")}>Quên mật khẩu?</button>
              <button onClick={() => switchMode("register")}>Chưa có tài khoản? Đăng ký</button>
            </>
          )}
          {mode === "register" && (
            <button onClick={() => switchMode("login")}>Đã có tài khoản? Đăng nhập</button>
          )}
          {mode === "forgot" && (
            <button onClick={() => switchMode("login")}>Quay lại đăng nhập</button>
          )}
        </div>
      </div>
    </div>
  );
}

function errorMessage(err: Error): string {
  // request() throws "<status>: <server detail json>" - the JSON body's
  // "detail" field is what the user should actually read.
  const match = err.message.match(/^\d+:\s*(.*)$/s);
  if (!match) return err.message;
  try {
    const parsed = JSON.parse(match[1]);
    return typeof parsed.detail === "string" ? parsed.detail : err.message;
  } catch {
    return match[1];
  }
}

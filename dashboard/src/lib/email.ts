// Mirrors dashboard_api/auth.py (EMAIL_PATTERN, GMAIL_PATTERN) so a form can
// say what is wrong before a round trip. The server re-checks both; these are
// a convenience, never the gate.

const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const GMAIL = /^(?=.{6,30}@)[a-z0-9](?:[a-z0-9]|\.(?!\.))*[a-z0-9]@gmail\.com$/;

export function normalizeEmail(value: string): string {
  return value.trim().toLowerCase();
}

/** Any address: what logging in and "Quên mật khẩu" accept. */
export function emailProblem(value: string): string | null {
  const email = normalizeEmail(value);
  if (!email) return null;
  return email.length <= 254 && EMAIL.test(email) ? null : "Email không hợp lệ.";
}

/** A new account: a Gmail address, by Gmail's own username rules. */
export function gmailProblem(value: string): string | null {
  const email = normalizeEmail(value);
  if (!email) return null;
  if (!email.endsWith("@gmail.com")) return "Tài khoản phải là địa chỉ Gmail (vd: tenban@gmail.com).";
  return GMAIL.test(email)
    ? null
    : "Phần trước @gmail.com cần 6-30 ký tự, chỉ gồm chữ, số và dấu chấm (không đứng đầu/cuối, không liền nhau).";
}

from enum import Enum
from urllib.parse import urlparse


class AuthState(str, Enum):
    UNKNOWN = "UNKNOWN"
    LOGGED_IN = "LOGGED_IN"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_RATE_LIMITED = "AUTH_RATE_LIMITED"
    SECURITY_CHALLENGE = "SECURITY_CHALLENGE"
    TEMPORARY_ERROR = "TEMPORARY_ERROR"


RATE_LIMIT_MARKERS = (
    "maximum number of attempts reached",
    "too many attempts",
    "too many requests",
    "số lần thử tối đa",
    "quá nhiều lần thử",
)

SECURITY_CHALLENGE_MARKERS = (
    "captcha",
    "verify to continue",
    "security verification",
    "security check",
    "one-time password",
    "verification code",
    "enter otp",
    "suspicious activity",
    "account restricted",
    "xác minh bảo mật",
    "mã xác minh",
    "hoạt động đáng ngờ",
    "tài khoản bị hạn chế",
)

TEMPORARY_ERROR_MARKERS = (
    "something went wrong",
    "temporarily unavailable",
    "service unavailable",
    "network error",
    "đã xảy ra lỗi",
    "tạm thời không khả dụng",
    "lỗi mạng",
)


def detect_auth_state(snapshot: dict) -> AuthState:
    """Classify auth using non-secret browser signals only."""
    content = f"{snapshot.get('title', '')}\n{snapshot.get('body_text', '')}".casefold()
    if any(marker in content for marker in RATE_LIMIT_MARKERS):
        return AuthState.AUTH_RATE_LIMITED
    if any(marker in content for marker in SECURITY_CHALLENGE_MARKERS):
        return AuthState.SECURITY_CHALLENGE
    if any(marker in content for marker in TEMPORARY_ERROR_MARKERS):
        return AuthState.TEMPORARY_ERROR

    parsed = urlparse(snapshot.get("url", ""))
    path = parsed.path.rstrip("/") or "/"
    login_form = bool(snapshot.get("login_form_visible"))
    has_session_cookie = bool(snapshot.get("session_cookie_names"))
    authenticated_dom = bool(snapshot.get("authenticated_dom"))
    page_loading = bool(snapshot.get("page_loading"))
    protected_route = path == "/messages" or path.startswith("/messages/")

    if path.startswith("/login") or login_form:
        return AuthState.AUTH_REQUIRED
    # A protected URL alone is never proof of authentication. TikTok may show
    # a login wall or partial shell at that route.
    if protected_route:
        if has_session_cookie and authenticated_dom:
            return AuthState.LOGGED_IN
        if has_session_cookie and page_loading:
            return AuthState.TEMPORARY_ERROR
        return AuthState.AUTH_REQUIRED if not has_session_cookie else AuthState.UNKNOWN
    if has_session_cookie and authenticated_dom:
        return AuthState.LOGGED_IN
    if parsed.hostname and (
        parsed.hostname == "tiktok.com" or parsed.hostname.endswith(".tiktok.com")
    ):
        return AuthState.UNKNOWN if has_session_cookie else AuthState.AUTH_REQUIRED
    return AuthState.UNKNOWN

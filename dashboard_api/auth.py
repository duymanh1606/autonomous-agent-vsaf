"""Session auth for this dashboard's own operator accounts.

Email+password login with a JWT session cookie. `install_middleware` reads
the cookie once per request and stashes the user in `current_user_var` (a
ContextVar, visible to the route handler that runs inside the same request
context) so the ~20 existing account-scoped routes - which all already
funnel through `_account_or_404` in dashboard_api/main.py - gain per-user
workspace permission checks without each one needing its own `Depends`.
"""

import logging
import os
import re
import secrets
import time
from contextvars import ContextVar
from threading import Lock
from typing import Any
from urllib.parse import urlparse

import bcrypt
import jwt
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from dashboard_api import security_config

from dashboard_api.users import UserStore

logger = logging.getLogger(__name__)

SESSION_COOKIE = "session"
# The window `exp` and the cookie's own `max_age` share, so a cookie never
# outlives the token inside it (which would surface as a confusing 401
# instead of a clean logout). Configurable via DASHBOARD_SESSION_TTL_SECONDS.
SESSION_MAX_AGE = security_config.DEFAULT_SESSION_TTL_SECONDS
JWT_ALGORITHM = "HS256"
# Tolerance for `exp` only, never for the revocation watermark - see
# session_revoked() for why that one gets no slack.
SESSION_CLOCK_LEEWAY_SECONDS = 30

# Requests to these paths are allowed through without a session - everything
# else under /api/ requires a logged-in user.
PUBLIC_PATHS = {
    "/api/auth/register",
    "/api/auth/login",
    "/api/auth/forgot-password",
    "/api/auth/reset-password",
}

# All a session may reach while its user still has to replace a temporary
# password.
MUST_CHANGE_PASSWORD_PATHS = {
    "/api/auth/me",
    "/api/auth/change-password",
    "/api/auth/logout",
}

# Methods that change state and therefore need an Origin they can be trusted
# to have come from. GET/HEAD/OPTIONS are excluded: they are not supposed to
# mutate anything, and OPTIONS must stay answerable for CORS preflight.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _load_secret_key() -> str:
    configured = os.environ.get("DASHBOARD_SECRET_KEY", "").strip()
    if configured:
        return configured
    generated = secrets.token_hex(32)
    logger.warning(
        "[auth] DASHBOARD_SECRET_KEY is not set - using a random key for "
        "this process. Every session will be invalidated on restart; set "
        "DASHBOARD_SECRET_KEY in .env for sessions that survive a restart."
    )
    return generated


SECRET_KEY = _load_secret_key()

current_user_var: ContextVar[dict[str, Any] | None] = ContextVar(
    "current_user", default=None
)


# Accounts are email addresses: "Quên mật khẩu" mails a temporary password to
# it, so it has to be one. This is the loose shape every login is checked
# against; creating an account adds the stricter Gmail rule below. Stored
# lowercased.
EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
EMAIL_MAX_LENGTH = 254


def normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


def email_error(email: str) -> str | None:
    """Why `email` (already normalised) is not a usable address, or None."""
    if len(email) <= EMAIL_MAX_LENGTH and EMAIL_PATTERN.fullmatch(email):
        return None
    return "email không hợp lệ"


# New accounts must be Gmail addresses - that is the mailbox "Quên mật khẩu"
# sends a temporary password to, so an address nobody can receive mail at
# would be an account nobody can recover. Gmail's own rules for a username:
# 6-30 characters of letters, digits and dots, starting and ending with a
# letter or digit, no two dots in a row. "+" aliases are refused on purpose:
# "an+1@gmail.com" and "an+2@gmail.com" are one inbox, and would otherwise be
# two accounts. Only creating an account applies this; logging in and
# "Quên mật khẩu" accept any address, so accounts made before this rule (an
# admin on the shop's own domain, say) keep working.
GMAIL_PATTERN = re.compile(r"(?=.{6,30}@)[a-z0-9](?:[a-z0-9]|\.(?!\.))*[a-z0-9]@gmail\.com")


def gmail_error(email: str) -> str | None:
    """Why `email` (already normalised) cannot be a new account, or None."""
    if GMAIL_PATTERN.fullmatch(email):
        return None
    return (
        "tài khoản phải là địa chỉ Gmail hợp lệ (vd: tenban@gmail.com): 6-30 ký "
        "tự gồm chữ, số và dấu chấm trước @gmail.com"
    )


# Temporary password mailed by "Quên mật khẩu". Short-lived, and it does not
# replace the real password until it is used: otherwise anyone could lock
# someone out just by asking for a reset on their address.
TEMP_PASSWORD_TTL_SECONDS = 30 * 60
# No 0/O, 1/l/I: it is read off an email and typed by hand.
_TEMP_PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_temp_password(length: int = 12) -> str:
    return "".join(secrets.choice(_TEMP_PASSWORD_ALPHABET) for _ in range(length))


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def create_session_token(user_id: int) -> str:
    """Mint a session token that expires on its own and can be revoked.

    `iat` is not decoration: it is what `session_revoked()` compares against
    the user's `sessions_valid_from` watermark, which is how a password change
    kills tokens that were already handed out.
    """
    # Sub-second resolution, and not by accident. A whole-second `iat`
    # compared against a whole-second watermark means a token minted at
    # 12:00:00.2 survives a password change at 12:00:00.7, because both
    # truncate to the same second - revocation silently failing for anyone
    # unlucky enough to be inside the same second. NumericDate allows a
    # fractional value, so both sides keep their fraction and the comparison
    # is exact.
    issued_at = time.time()
    return jwt.encode(
        {
            "sub": str(user_id),
            "iat": issued_at,
            "exp": issued_at + security_config.session_ttl_seconds(),
        },
        SECRET_KEY, algorithm=JWT_ALGORITHM,
    )


def decode_session(token: str) -> tuple[int, float] | None:
    """`(user_id, issued_at)` for a valid token, else None.

    All three claims are required, so a token minted before this code existed
    (no `iat`, no `exp`) is refused rather than silently treated as eternal.
    """
    try:
        payload = jwt.decode(
            token, SECRET_KEY, algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "iat", "exp"]},
            leeway=SESSION_CLOCK_LEEWAY_SECONDS,
        )
        return int(payload["sub"]), float(payload["iat"])
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        return None


def decode_session_token(token: str) -> int | None:
    """Just the user id. Kept as its own function because several tests patch
    this name and expect a bare int; new callers want `decode_session`."""
    decoded = decode_session(token)
    return decoded[0] if decoded else None


def session_revoked(row: dict[str, Any] | None, issued_at: float | None) -> bool:
    """True when this token predates the user's revocation watermark.

    No positive tolerance, on purpose. "Tolerance" here would mean a token
    minted a few seconds before a password change survives it - i.e. exactly
    the hole the watermark exists to close. The legitimate race (revoke, then
    immediately mint a replacement) is handled by ordering instead: callers
    bump the watermark *first* and set the new cookie *after*, so the
    replacement's `iat` is >= the watermark by construction.

    `.get` rather than `[...]`: several existing tests patch `get_by_id` with
    a minimal row containing only `id` and `is_active`, and they have nothing
    to do with revocation.
    """
    if row is None or issued_at is None:
        return False
    valid_from = row.get("sessions_valid_from")
    if valid_from is None:
        return False
    return issued_at < valid_from.timestamp()


def set_session_cookie(response, user_id: int, request: Request | None = None) -> None:
    # `secure=True` only over HTTPS - otherwise a browser refuses to send the
    # cookie at all, which would break both a plain-HTTP loopback deployment
    # and the `ssh -L` operator path. Two sources say "this is HTTPS":
    #
    #  * production, which is fail-closed: an internet-facing origin must not
    #    depend on a header to decide, and the Cloudflare Tunnel path is
    #    always HTTPS from the browser's side;
    #  * X-Forwarded-Proto, which the nginx in front of us sets per server
    #    block (hardcoded `https` on the tunnel listener, `$scheme` on the
    #    local one) - a client could forge it, but the only thing that buys
    #    them is a stricter cookie for themselves.
    #
    # `request=None` (a unit test constructing a bare Response) keeps today's
    # behaviour rather than guessing.
    forwarded_https = bool(
        request is not None
        and request.headers.get("x-forwarded-proto", "").lower() == "https"
    )
    secure = security_config.is_production() or forwarded_https
    # `path` is explicit on both the set and the delete below: they currently
    # agree only because both default to "/", and the day someone scopes one
    # of them the logout stops working silently.
    response.set_cookie(
        SESSION_COOKIE, create_session_token(user_id),
        max_age=security_config.session_ttl_seconds(),
        httponly=True, samesite="lax", secure=secure, path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def public_user(user: dict[str, Any], account_ids: list[str]) -> dict[str, Any]:
    return {
        "id": user["id"], "email": user["email"], "is_admin": user["is_admin"],
        "account_ids": account_ids,
        # How many workspaces this user may create. NULL/absent means none -
        # see workspace_limit() in dashboard_api/main.py, which is the only
        # place that turns this into a decision. Read fresh from the users
        # row on every request by install_middleware below, so an admin
        # changing it takes effect on the user's very next request, with no
        # re-login and no stale session copy.
        "max_workspaces": user.get("max_workspaces"),
        # Signed in with a mailed temporary password: nothing but choosing a
        # new one is allowed until they have (see install_middleware).
        "must_change_password": bool(user.get("must_change_password")),
    }


def require_current_user() -> dict[str, Any]:
    user = current_user_var.get()
    if user is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


def require_admin() -> dict[str, Any]:
    user = require_current_user()
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="admin only")
    return user


class RateLimiter:
    """Fixed-window attempt counter, keyed by an arbitrary string (email or
    client IP). In-memory only - resets on a supervisor restart, same
    documented tradeoff as the process registry in dashboard_api/processes.py.
    That's acceptable here: a restart-triggered reset just gives an attacker
    a few more free tries, it doesn't remove the limit entirely.
    """

    def __init__(self, max_attempts: int, window_seconds: float):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._hits: dict[str, list[float]] = {}
        self._lock = Lock()

    def _prune(self, key: str, now: float) -> list[float]:
        hits = [t for t in self._hits.get(key, ()) if now - t < self.window_seconds]
        if hits:
            self._hits[key] = hits
        else:
            # Drop the key rather than leaving an empty list behind. A spray
            # across many distinct keys (one per attacker-chosen IP) would
            # otherwise grow this dict without bound - a slow memory leak on
            # the very code path that is supposed to contain the abuse.
            self._hits.pop(key, None)
        return hits

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            hits = self._prune(key, now)
            if len(hits) >= self.max_attempts:
                retry_after = int(self.window_seconds - (now - hits[0])) + 1
                raise HTTPException(
                    status_code=429,
                    detail=f"Quá nhiều lần thử - thử lại sau {retry_after} giây",
                    headers={"Retry-After": str(retry_after)},
                )

    def hit(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            hits = self._prune(key, now)
            hits.append(now)
            self._hits[key] = hits

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


# Login: 5 failed attempts per email within 5 minutes locks that email out -
# a successful login clears its counter. Deliberately keyed by email, not
# IP, so a shared office IP (multiple real operators) never locks everyone
# out over one person's typos.
login_limiter = RateLimiter(max_attempts=5, window_seconds=300)

# ...but an email-only limit leaves password spraying wide open: one IP can
# try a single common password against thousands of addresses and never trip
# a counter. These two add the missing axes. Note that every operator coming
# in over an `ssh -L` tunnel shares the single "127.0.0.1" bucket, because
# client_ip() refuses to believe forwarding headers from a loopback peer that
# Cloudflare Access has not vouched for - that is intended, not a bug, and it
# is why the per-IP budget is generous.
login_ip_limiter = RateLimiter(max_attempts=20, window_seconds=300)
# A blunt ceiling for a spray distributed across many source IPs. High enough
# that a busy shift of real operators never sees it.
login_global_limiter = RateLimiter(max_attempts=200, window_seconds=300)
LOGIN_GLOBAL_KEY = "global"

# Registration: 5 signups per source IP per hour - throttles scripted mass
# account creation without needing to close self-registration entirely
# (new accounts start with zero workspace access until an admin grants it,
# so a stray signup is low-stakes; unlimited automated signups is not).
register_limiter = RateLimiter(max_attempts=5, window_seconds=3600)

# Temporary passwords: at most 3 per address per hour, for the same reason.
temp_password_email_limiter = RateLimiter(max_attempts=3, window_seconds=3600)

# Change-password (signed in): 5 wrong "current password" guesses per user
# within 5 minutes. A session left open on a shared machine must not become
# an unlimited oracle for that account's password - which matters because the
# same password is often reused elsewhere.
change_password_limiter = RateLimiter(max_attempts=5, window_seconds=300)

# Forgot-password: also public and also a mass-mail vector (spamming
# arbitrary addresses with reset emails) if left unthrottled.
forgot_password_limiter = RateLimiter(max_attempts=5, window_seconds=3600)


def client_ip(request: Request) -> str:
    """The caller's address, or the closest trustworthy thing to it.

    X-Forwarded-For is never read. Both nginx layers use
    `proxy_add_x_forwarded_for`, which *appends*, so the left-most entry is
    whatever the client typed - reading it made every IP-keyed rate limit
    bypassable with one header. Counting hops from the right is no better:
    with two nginx layers plus a cloudflared hop the count is not stable, and
    a wrong count is worse than no header at all.

    What is left is two honest cases:

      * the peer is not a trusted proxy - it is talking to the supervisor
        directly, so its own address is the answer and nothing it claims
        about itself matters;
      * the peer is a trusted proxy and Cloudflare Access has verified this
        request, in which case CF-Connecting-IP was written by the Cloudflare
        edge (which overwrites any client-supplied value) and is real.

    Everything else - notably the `ssh -L` operator path - collapses to the
    loopback address and therefore shares one rate-limit bucket. Reaching
    loopback already costs an SSH credential, so that is an acceptable trade;
    see the limiter budgets above.
    """
    peer = request.client.host if request.client else "unknown"
    if peer not in security_config.trusted_proxies():
        return peer
    if getattr(request.state, "cf_access_verified", False):
        edge_ip = (request.headers.get("cf-connecting-ip") or "").strip()
        if edge_ip:
            return edge_ip
    return peer


def check_account_access(account_id: str) -> None:
    """Raise 403 unless the current request's user is an admin or has been
    granted this workspace. No-op if no auth middleware ran (e.g. a direct
    unit test of a store) - the middleware is what actually enforces login."""
    user = current_user_var.get()
    if user is None or user.get("is_admin"):
        return
    if account_id not in user.get("account_ids", []):
        raise HTTPException(status_code=403, detail="no access to this workspace")


def request_origin(headers) -> str:
    """The origin a request claims to come from, as `scheme://host[:port]`.

    `Origin` is what browsers send on state-changing fetches and on WebSocket
    handshakes. `Referer` is the fallback for the handful of cases where a
    browser sends only that; anything after the origin is stripped.
    """
    origin = (headers.get("origin") or "").strip()
    if origin and origin.lower() != "null":
        return origin.rstrip("/")
    referer = (headers.get("referer") or "").strip()
    if referer:
        parsed = urlparse(referer)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def origin_allowed(headers) -> bool:
    """Whether a state-changing request may proceed on Origin grounds.

    Enforced only once `DASHBOARD_TRUSTED_ORIGINS` (or DASHBOARD_PUBLIC_URL)
    names something: configuring the allowlist is how an operator opts in.
    Without that, every `TestClient` call in the suite - none of which send an
    `Origin` - and every curl over an SSH tunnel would start failing, which is
    a worse outcome than the CSRF exposure this closes on a loopback-only box.

    Inside the enforced mode a missing Origin *is* a rejection: browsers
    always send one on these methods, so its absence means a non-browser
    client, and those should be talking to the loopback port instead.
    """
    if not security_config.csrf_enabled():
        return True
    return request_origin(headers) in security_config.trusted_origins()


def install_middleware(app, users_store: UserStore) -> None:
    @app.middleware("http")
    async def _auth_middleware(request: Request, call_next):
        token = request.cookies.get(SESSION_COOKIE)
        user = None
        if token:
            decoded = decode_session(token)
            if decoded is not None:
                user_id, issued_at = decoded
                row = users_store.get_by_id(user_id)
                # A deactivated account is logged out on its very next request,
                # not just refused a fresh login - no need to wait for its
                # existing session cookie to expire. The same is true of a
                # revoked one: a password change invalidates tokens already in
                # the wild, which `delete_cookie` alone never could.
                if (
                    row is not None
                    and row["is_active"]
                    and not session_revoked(row, issued_at)
                ):
                    user = public_user(row, users_store.accessible_account_ids(user_id))
        reset_token = current_user_var.set(user)
        try:
            path = request.url.path
            if path.startswith("/api/") and path not in PUBLIC_PATHS and user is None:
                return JSONResponse({"detail": "not authenticated"}, status_code=401)
            # Enforced here, not only by the screen the dashboard shows: a
            # temporary password came through a mailbox, so until it is
            # replaced the session it opened reaches nothing else.
            if (
                user is not None
                and user.get("must_change_password")
                and path.startswith("/api/")
                and path not in MUST_CHANGE_PASSWORD_PATHS
            ):
                return JSONResponse(
                    {"detail": "cần đặt mật khẩu mới trước khi tiếp tục"}, status_code=403,
                )
            return await call_next(request)
        finally:
            current_user_var.reset(reset_token)


def install_csrf_middleware(app) -> None:
    """Reject cross-site state-changing requests by Origin.

    `SameSite=Lax` on the session cookie already stops most of this, but it is
    a control that lives in the browser and says nothing about the WebSocket
    handshake (checked separately, in the /api/events handler). This is the
    origin-side half.
    """
    @app.middleware("http")
    async def _csrf_middleware(request: Request, call_next):
        if (
            request.method in UNSAFE_METHODS
            and request.url.path.startswith("/api/")
            and not origin_allowed(request.headers)
        ):
            logger.warning(
                "[auth] rejected %s %s: origin=%r not trusted",
                request.method, request.url.path, request_origin(request.headers),
            )
            return JSONResponse(
                {"detail": "origin không được phép"}, status_code=403,
            )
        return await call_next(request)

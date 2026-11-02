"""Web Push: desktop/phone notifications that arrive with the dashboard closed.

The in-page notifications (dashboard/src/lib/browserNotifications.ts) only
exist while a dashboard tab is open. This is the other half: a browser that
switched notifications on registers a push subscription here, and when one of
a few events happens the supervisor sends it through the browser vendor's
push service (Google, Mozilla, Apple, Microsoft); the dashboard's service
worker (dashboard/public/sw.js) shows it.

Who receives what follows the email path (dashboard_api/alerts.py): every
active user *granted* the workspace (its creator and the staff given access) -
never another shop's staff, and not an admin just for being one. The payload
carries the customer's name and the reason, never the customer's words.

Three things keep it honest:

* **Only fresh events.** An agent replays its latest event of each type to
  every new stream (control/events.py), so a supervisor restart re-forwards an
  escalation handled an hour ago. Anything older than FRESH_SECONDS is not
  news, and each (workspace, tag) is pushed at most once per REPEAT_SECONDS.
* **Only real push services.** The supervisor POSTs to whatever endpoint a
  browser registered; an endpoint on any other host would let a logged-in
  user make this server call an internal address. PUSH_HOSTS is the list.
* **Never in the event loop.** Delivery is an HTTPS request per browser, run
  on a small thread pool; the hub calls `on_event` synchronously.

The VAPID key pair (the server's identity towards push services) lives in
PostgreSQL, created once on first use, so a backup restored on a new machine
keeps every browser's subscription working. Its loss is not fatal either: a
push service then refuses with 401/403, the subscription is dropped, and the
dashboard re-subscribes the next time that browser opens it.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import RLock
from time import monotonic
from typing import Any
from urllib.parse import urlparse

import psycopg
from cryptography.hazmat.primitives import serialization
from psycopg.rows import dict_row
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from storage.database import resolve_dsn


logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    endpoint    TEXT NOT NULL UNIQUE,
    p256dh      TEXT NOT NULL,
    auth        TEXT NOT NULL,
    user_agent  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_ok_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_push_subscriptions_user ON push_subscriptions(user_id);
CREATE TABLE IF NOT EXISTS web_push_vapid (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    private_pem TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

# Host suffixes of the browser vendors' push services. A subscription naming
# any other host is refused when it is registered.
PUSH_HOSTS = (
    "fcm.googleapis.com",            # Chrome, Edge on Android, Opera, Brave
    "push.services.mozilla.com",     # Firefox
    "notify.windows.com",            # Edge (WNS)
    "push.apple.com",                # Safari, iOS/iPadOS home-screen apps
)

FRESH_SECONDS = 60
REPEAT_SECONDS = 600
# How long a push service keeps a message for a browser that is offline:
# an hour-old "customer waiting" still matters, a day-old one does not.
TTL_SECONDS = 3600
SEND_TIMEOUT_SECONDS = 10
MAX_ENDPOINT_LENGTH = 1024
MAX_KEY_LENGTH = 256


def endpoint_allowed(endpoint: str) -> bool:
    try:
        parsed = urlparse(endpoint)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and len(endpoint) <= MAX_ENDPOINT_LENGTH
        and any(host == item or host.endswith("." + item) for item in PUSH_HOSTS)
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PushStore:
    """Subscriptions per dashboard user, and the server's VAPID key."""

    def __init__(self, dsn: str | None = None):
        self.dsn = str(dsn or resolve_dsn())
        self._conn: psycopg.Connection[dict[str, Any]] | None = None
        self._lock = RLock()
        self._vapid: Vapid | None = None

    def connect(self) -> psycopg.Connection[dict[str, Any]]:
        with self._lock:
            if self._conn is None or self._conn.closed:
                self._conn = psycopg.connect(
                    self.dsn, row_factory=dict_row, autocommit=True,
                    connect_timeout=5,
                )
                self._conn.execute(SCHEMA)
            return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None and not self._conn.closed:
                self._conn.close()
            self._conn = None

    # ------------------------------------------------------------- VAPID
    def vapid(self) -> Vapid:
        with self._lock:
            if self._vapid is None:
                fresh = Vapid()
                fresh.generate_keys()
                pem = fresh.private_pem().decode("ascii")
                conn = self.connect()
                # Two supervisors racing on first use both insert; the first
                # wins and both read back the same key.
                conn.execute(
                    "INSERT INTO web_push_vapid (id, private_pem) VALUES (1, %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (pem,),
                )
                row = conn.execute(
                    "SELECT private_pem FROM web_push_vapid WHERE id = 1"
                ).fetchone()
                if row is None:
                    raise RuntimeError("web_push_vapid has no key after inserting one")
                self._vapid = Vapid.from_pem(row["private_pem"].encode("ascii"))
            return self._vapid

    def public_key(self) -> str:
        """The key a browser passes to pushManager.subscribe, base64url."""
        raw = self.vapid().public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
        )
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    # ------------------------------------------------------ subscriptions
    def save(self, *, user_id: int, endpoint: str, p256dh: str, auth: str,
             user_agent: str = "") -> None:
        """One row per browser. A browser handed to another person (someone
        else logs in on it) moves to that person rather than keeping both."""
        with self._lock:
            self.connect().execute(
                """INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, user_agent)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (endpoint) DO UPDATE SET
                     user_id = excluded.user_id, p256dh = excluded.p256dh,
                     auth = excluded.auth, user_agent = excluded.user_agent""",
                (user_id, endpoint, p256dh, auth, user_agent[:300]),
            )

    def delete(self, endpoint: str, *, user_id: int | None = None) -> bool:
        clause = "endpoint = %s"
        params: list[object] = [endpoint]
        if user_id is not None:
            clause += " AND user_id = %s"
            params.append(user_id)
        with self._lock:
            cursor = self.connect().execute(
                f"DELETE FROM push_subscriptions WHERE {clause}", params,
            )
            return cursor.rowcount > 0

    def for_user(self, user_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connect().execute(
                "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = %s",
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def for_account(self, account_id: str) -> list[dict[str, Any]]:
        """Every browser of every active user granted this workspace."""
        with self._lock:
            rows = self.connect().execute(
                """SELECT DISTINCT sub.endpoint, sub.p256dh, sub.auth
                     FROM push_subscriptions AS sub
                     JOIN users ON users.id = sub.user_id
                     JOIN user_account_access AS access
                       ON access.user_id = sub.user_id AND access.account_id = %s
                    WHERE users.is_active""",
                (account_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_ok(self, endpoint: str) -> None:
        with self._lock:
            self.connect().execute(
                "UPDATE push_subscriptions SET last_ok_at = %s WHERE endpoint = %s",
                (_now(), endpoint),
            )


# ------------------------------------------------------------- content
# Same titles as the in-page notifications (browserNotifications.ts), minus
# the customer's words: those stay on the dashboard.
_FIXED = {
    "auth.required": (
        "Mất phiên đăng nhập TikTok", "Workspace đã bị đăng xuất khỏi TikTok, cần đăng nhập lại.",
    ),
    "auth.security_challenge": (
        "TikTok yêu cầu xác minh", "Cần giải CAPTCHA/xác minh bảo mật bằng tay qua màn hình Chrome.",
    ),
    "auth.rate_limited": ("TikTok tạm giới hạn đăng nhập", "Đợi một lúc rồi thử lại."),
    "workspace.degraded": (
        "Workspace không tự đánh thức được",
        "Workspace đang ngủ và đánh thức thất bại nhiều lần. Kiểm tra và Start lại.",
    ),
}


def describe(event: dict[str, Any], workspace: str) -> dict[str, Any] | None:
    """The notification for an event, or None for one not worth a push."""
    from dashboard_api.notifications import ESCALATION_REASONS

    event_type = str(event.get("type") or "")
    account_id = str(event.get("account_id") or "")
    payload = event.get("payload") or {}
    base = {"account_id": account_id, "url": "/"}
    if event_type == "escalation.opened":
        who = str(payload.get("conversation") or "").strip() or "Một khách hàng"
        conversation_id = str(payload.get("conversation_id") or "").strip()
        reason = str(payload.get("reason") or "")
        known = ESCALATION_REASONS.get(reason)
        return {
            **base,
            "title": f"Chờ tiếp quản - {workspace}",
            "body": f"{who}: {known[0]}" if known else f"{who} đang chờ nhân viên tiếp quản.",
            # One per conversation: a second escalation of the same customer
            # replaces the first popup instead of stacking.
            "tag": f"{account_id}:{event_type}:{conversation_id or who}",
            "conversation_id": conversation_id or None,
        }
    fixed = _FIXED.get(event_type)
    if fixed is None:
        return None
    title, body = fixed
    return {**base, "title": f"{title} - {workspace}", "body": body,
            "tag": f"{account_id}:{event_type}"}


def _age_seconds(event: dict[str, Any]) -> float | None:
    try:
        at = datetime.fromisoformat(str(event.get("ts") or ""))
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (_now() - at).total_seconds()


class WebPushNotifier:
    def __init__(self, store: PushStore, *, label_for=None, send=None, executor=None):
        self.store = store
        self.label_for = label_for or (lambda account_id: account_id)
        # Swapped in tests; looked up at call time otherwise.
        self._send = send
        self._executor = executor or ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="web-push",
        )
        self._recent: dict[str, float] = {}
        self._recent_lock = RLock()

    def on_event(self, event: dict[str, Any]) -> bool:
        """Called by the hub for every event. Returns whether a push was queued."""
        if not enabled():
            return False
        account_id = str(event.get("account_id") or "")
        if not account_id:
            return False
        age = _age_seconds(event)
        if age is None or age > FRESH_SECONDS:
            return False
        content = describe(event, self.label_for(account_id))
        if content is None or not self._first_in_a_while(content["tag"]):
            return False
        self._executor.submit(self._deliver_to_account, account_id, content)
        return True

    def _first_in_a_while(self, tag: str) -> bool:
        now = monotonic()
        with self._recent_lock:
            for key, at in list(self._recent.items()):
                if now - at > REPEAT_SECONDS:
                    del self._recent[key]
            if tag in self._recent:
                return False
            self._recent[tag] = now
            return True

    def _deliver_to_account(self, account_id: str, content: dict[str, Any]) -> int:
        try:
            return self.deliver(self.store.for_account(account_id), content)
        except Exception:
            logger.exception("[push] delivering to workspace=%s failed", account_id)
            return 0

    def send_test(self, user_id: int) -> int:
        """Push a sample to every browser of one user. Returns how many took it."""
        return self.deliver(self.store.for_user(user_id), {
            "title": "Thông báo thử",
            "body": "Thông báo đẩy đang hoạt động - bạn sẽ nhận được cả khi đã đóng dashboard.",
            "tag": "dashboard:test", "url": "/",
        })

    def deliver(self, subscriptions, content: dict[str, Any]) -> int:
        delivered = 0
        if not subscriptions:
            return 0
        data = json.dumps(content, ensure_ascii=False)
        vapid = self.store.vapid()
        send = self._send or webpush
        for sub in subscriptions:
            endpoint = sub["endpoint"]
            try:
                send(
                    subscription_info={
                        "endpoint": endpoint,
                        "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
                    },
                    data=data,
                    vapid_private_key=vapid,
                    vapid_claims={"sub": vapid_subject()},
                    ttl=TTL_SECONDS,
                    timeout=SEND_TIMEOUT_SECONDS,
                    headers={"Urgency": "high"},
                )
            except WebPushException as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                # 404/410: the browser dropped the subscription. 401/403: it
                # was made for a VAPID key this server no longer has. Either
                # way it can never work again; the dashboard re-subscribes
                # the next time that browser opens it.
                if status in (401, 403, 404, 410):
                    self.store.delete(endpoint)
                    logger.info("[push] subscription gone (HTTP %s), removed", status)
                else:
                    # The endpoint carries the browser's push id: not logged.
                    logger.warning(
                        "[push] send failed host=%s status=%s",
                        urlparse(endpoint).hostname, status, exc_info=True,
                    )
                continue
            except Exception:
                logger.exception("[push] send failed host=%s", urlparse(endpoint).hostname)
                continue
            delivered += 1
            self.store.mark_ok(endpoint)
        return delivered


def enabled() -> bool:
    return os.environ.get("WEB_PUSH", "on").strip().lower() not in ("0", "off", "false", "no")


def vapid_subject() -> str:
    """Who to contact about this sender, as push services require.

    Apple refuses a placeholder, so the dashboard's own https address or the
    mail sender is used when there is one.
    """
    url = os.environ.get("DASHBOARD_PUBLIC_URL", "").strip()
    if url.startswith("https://"):
        return url
    sender = (
        os.environ.get("SMTP_FROM", "").strip() or os.environ.get("SMTP_USERNAME", "").strip()
    )
    if "@" in sender:
        return f"mailto:{sender}"
    return "mailto:admin@example.com"

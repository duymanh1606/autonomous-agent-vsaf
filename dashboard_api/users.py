"""Dashboard operator accounts: email+password login, per-workspace access
grants, and password-reset tokens.

Distinct from `AccountRegistry` (dashboard_api/accounts.py), which tracks
TikTok Shop workspaces (`account_id`) - these rows are the human operators
who log into this dashboard, and which of those workspaces each one may see.
Uses the same lazy self-connecting PostgreSQL pattern as NotificationStore/
OrderStore (dashboard_api/notifications.py, dashboard_api/orders.py).
"""

import secrets
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any

import psycopg
from psycopg.rows import dict_row

from storage.database import resolve_dsn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_admin      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
-- How many workspaces this user may own (create). NULL means "an admin has
-- not granted any", and is read as 0 - not as "unlimited", which is what an
-- earlier build meant by it and is why a self-registered stranger could spawn
-- a Chrome and an agent process per workspace on the host. The quota is
-- fail-closed now: a row nobody has touched creates nothing. Admins ignore
-- the column entirely - enforced in dashboard_api/main.py's create_account,
-- not here.
ALTER TABLE users ADD COLUMN IF NOT EXISTS max_workspaces INTEGER;
-- Session revocation watermark. A session token carries the second it was
-- minted (`iat`); any token minted before this moment is refused. That is
-- what makes "change the password and every stolen cookie dies" true without
-- keeping a server-side session table. Existing rows get CURRENT_TIMESTAMP
-- once, which is harmless: the same deploy that adds this column also starts
-- requiring `iat`, so every pre-existing token is refused anyway.
ALTER TABLE users ADD COLUMN IF NOT EXISTS sessions_valid_from TIMESTAMPTZ
    NOT NULL DEFAULT CURRENT_TIMESTAMP;
-- When an admin first approved this account. NULL means "never approved",
-- which is what distinguishes a signup nobody has looked at yet from an
-- employee who was deliberately switched off - the admin UI shows them
-- differently, and missing a pending signup is the failure that matters.
ALTER TABLE users ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;
-- Backfill, once: every account that already exists and works was approved
-- by definition. Pending signups are inactive, so this never touches them,
-- and re-running it on the next connect is a no-op.
UPDATE users SET approved_at = created_at
 WHERE approved_at IS NULL AND is_active = TRUE;
CREATE TABLE IF NOT EXISTS user_account_access (
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    account_id  TEXT NOT NULL,
    granted_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, account_id)
);
-- "Quên mật khẩu" mails a temporary password. It sits beside the real one
-- (which keeps working) until it is used or expires, so asking for a reset on
-- somebody else's address cannot lock them out. Using it swaps it in as the
-- password and sets must_change_password: that session can do nothing but
-- choose a new one.
ALTER TABLE users ADD COLUMN IF NOT EXISTS temp_password_hash TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS temp_password_expires_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN
    NOT NULL DEFAULT FALSE;
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    token       TEXT PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ
);
"""

RESET_TOKEN_TTL = timedelta(hours=1)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"], "email": row["email"], "is_admin": row["is_admin"],
        "is_active": row["is_active"], "created_at": row["created_at"],
        # "waiting for a human", as opposed to "a human switched this off".
        "is_pending": not row["is_active"] and row.get("approved_at") is None,
        # NULL stays NULL here rather than being coerced to 0: the admin UI
        # shows "chưa cấp" differently from a limit deliberately set to 0.
        # Everything that *decides* anything goes through
        # dashboard_api/main.py's workspace_limit(), which reads both as 0.
        "max_workspaces": row.get("max_workspaces"),
    }


class UserStore:
    def __init__(self, dsn: str | None = None):
        self.dsn = str(dsn or resolve_dsn())
        self._conn = None
        self._lock = RLock()

    def connect(self):
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

    def count_users(self) -> int:
        with self._lock:
            row = self.connect().execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return int(row["n"]) if row else 0

    def create_user(
        self, email: str, password_hash: str, is_admin: bool,
        is_active: bool = True,
    ) -> dict[str, Any]:
        # `is_active` defaults to True so create_admin.py and every existing
        # caller keep their behaviour; self-registration passes False and the
        # account stays unusable until an admin approves it.
        with self._lock:
            row = self.connect().execute(
                """INSERT INTO users (email, password_hash, is_admin, is_active)
                   VALUES (%s, %s, %s, %s) RETURNING *""",
                (email, password_hash, is_admin, is_active),
            ).fetchone()
        return dict(row)

    def get_by_email(self, email: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connect().execute(
                "SELECT * FROM users WHERE email=%s", (email,),
            ).fetchone()
        return dict(row) if row else None

    def get_by_id(self, user_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self.connect().execute(
                "SELECT * FROM users WHERE id=%s", (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def accessible_account_ids(self, user_id: int) -> list[str]:
        with self._lock:
            rows = self.connect().execute(
                """SELECT account_id FROM user_account_access
                   WHERE user_id=%s ORDER BY granted_at""",
                (user_id,),
            ).fetchall()
        return [row["account_id"] for row in rows]

    def has_access(self, user_id: int, account_id: str) -> bool:
        with self._lock:
            row = self.connect().execute(
                "SELECT 1 FROM user_account_access WHERE user_id=%s AND account_id=%s",
                (user_id, account_id),
            ).fetchone()
        return row is not None

    def workspace_emails(self, account_ids) -> dict[str, list[str]]:
        """account_id -> the registered email of every active user granted it.

        The creator of a workspace is granted it when creating it, so this is
        its owner plus the staff given access. An admin is not on it just for
        being an admin: admins see every shop, and mailing them every shop's
        conversations is noise, not an alert.
        """
        result: dict[str, list[str]] = {}
        with self._lock:
            rows = self.connect().execute(
                """SELECT access.account_id, users.email
                     FROM user_account_access AS access
                     JOIN users ON users.id = access.user_id
                    WHERE access.account_id = ANY(%s) AND users.is_active
                 ORDER BY access.granted_at""",
                (list(account_ids),),
            ).fetchall()
        for row in rows:
            result.setdefault(row["account_id"], []).append(row["email"])
        return result

    def grant_access(self, user_id: int, account_id: str) -> None:
        with self._lock:
            self.connect().execute(
                """INSERT INTO user_account_access (user_id, account_id)
                   VALUES (%s, %s) ON CONFLICT (user_id, account_id) DO NOTHING""",
                (user_id, account_id),
            )

    def revoke_access(self, user_id: int, account_id: str) -> None:
        with self._lock:
            self.connect().execute(
                "DELETE FROM user_account_access WHERE user_id=%s AND account_id=%s",
                (user_id, account_id),
            )

    def list_users(self) -> list[dict[str, Any]]:
        with self._lock:
            conn = self.connect()
            rows = conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()
            result = []
            for row in rows:
                access = conn.execute(
                    """SELECT account_id FROM user_account_access
                       WHERE user_id=%s ORDER BY granted_at""",
                    (row["id"],),
                ).fetchall()
                result.append({
                    **_public(dict(row)),
                    "account_ids": [a["account_id"] for a in access],
                })
        return result

    def create_reset_token(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self.connect().execute(
                """INSERT INTO password_reset_tokens (token, user_id, expires_at)
                   VALUES (%s, %s, %s)""",
                (token, user_id, _now() + RESET_TOKEN_TTL),
            )
        return token

    def consume_reset_token(self, token: str) -> int | None:
        """Validates and burns a reset token in one step; returns the user_id
        it belonged to, or None if the token is unknown/expired/already used."""
        with self._lock:
            row = self.connect().execute(
                """UPDATE password_reset_tokens SET used_at=%s
                   WHERE token=%s AND used_at IS NULL AND expires_at > %s
                   RETURNING user_id""",
                (_now(), token, _now()),
            ).fetchone()
        return int(row["user_id"]) if row else None

    def update_password(self, user_id: int, password_hash: str) -> None:
        """Set the password, and with it end any temporary-password state: a
        password chosen (or set by an admin) supersedes a mailed one."""
        with self._lock:
            self.connect().execute(
                """UPDATE users
                      SET password_hash=%s, must_change_password=FALSE,
                          temp_password_hash=NULL, temp_password_expires_at=NULL
                    WHERE id=%s""",
                (password_hash, user_id),
            )

    # ------------------------------------------------- temporary passwords
    def set_temp_password(self, user_id: int, temp_hash: str, expires_at: datetime) -> None:
        with self._lock:
            self.connect().execute(
                """UPDATE users SET temp_password_hash=%s, temp_password_expires_at=%s
                    WHERE id=%s""",
                (temp_hash, expires_at, user_id),
            )

    def use_temp_password(self, user_id: int) -> None:
        """The temporary password was just used to log in: it becomes the
        password, can never be used as a temporary one again, and the user
        must replace it before doing anything else."""
        with self._lock:
            self.connect().execute(
                """UPDATE users
                      SET password_hash=temp_password_hash, must_change_password=TRUE,
                          temp_password_hash=NULL, temp_password_expires_at=NULL
                    WHERE id=%s AND temp_password_hash IS NOT NULL""",
                (user_id,),
            )

    def invalidate_sessions(self, user_id: int, at: datetime | None = None) -> None:
        """Refuse every session token minted before now for this user.

        The watermark comes from Python's clock, not `CURRENT_TIMESTAMP`, so
        that it and the `iat` in the tokens being invalidated are measured by
        the same clock - the supervisor and PostgreSQL are the same host
        today, but nothing in the code guarantees that.

        `GREATEST` keeps the watermark monotonic: two concurrent invalidations
        can never move it backwards and quietly revive a revoked token.
        """
        with self._lock:
            self.connect().execute(
                """UPDATE users
                      SET sessions_valid_from = GREATEST(sessions_valid_from, %s)
                    WHERE id = %s""",
                (at or _now(), user_id),
            )

    def set_admin(self, user_id: int, is_admin: bool) -> None:
        with self._lock:
            self.connect().execute(
                "UPDATE users SET is_admin=%s WHERE id=%s", (is_admin, user_id),
            )

    def set_active(self, user_id: int, is_active: bool) -> None:
        """Activating also records the approval, so a later deactivation
        cannot make an established account look like a fresh signup."""
        with self._lock:
            self.connect().execute(
                """UPDATE users
                      SET is_active = %s,
                          approved_at = CASE
                              WHEN %s AND approved_at IS NULL THEN %s
                              ELSE approved_at
                          END
                    WHERE id = %s""",
                (is_active, is_active, _now(), user_id),
            )

    def set_max_workspaces(self, user_id: int, max_workspaces: int | None) -> None:
        """0 (or None, which reads the same) forbids creating any new
        workspace; the ones the user already owns are untouched, because a
        limit caps creating and is never a reason to delete. Lowering it
        below what someone already owns is allowed on purpose: it stops the
        next one without taking away what is already running."""
        with self._lock:
            self.connect().execute(
                "UPDATE users SET max_workspaces=%s WHERE id=%s",
                (max_workspaces, user_id),
            )

    def delete_user(self, user_id: int) -> bool:
        with self._lock:
            cursor = self.connect().execute(
                "DELETE FROM users WHERE id=%s", (user_id,),
            )
            return cursor.rowcount > 0

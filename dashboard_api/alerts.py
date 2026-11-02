"""Alerting that reaches people who are not looking at the dashboard.

The health monitor and the event hub already turn trouble into
`notifications` rows - but a row only helps someone who opens the dashboard.
Two things here close that gap:

* **Email** (`AlertDispatcher`): a serious notification (`bad`/`error`) that is
  still open after a grace period goes out by mail through the SMTP setup the
  dashboard already uses (dashboard_api/mailer.py), and a second mail says when
  it has cleared. The grace is what keeps a routine agent restart or a slow
  render from paging anyone: only a fault that *lasts* is worth a mail.
* **Heartbeat** (`ping_heartbeat`): every completed monitor sweep pings a URL
  outside this machine (Healthchecks.io, UptimeRobot heartbeat, Better Stack…).
  That service alerts when the pings stop - the one failure nothing on this
  server can report: the server, or the supervisor, being down.

Configured from the environment (.env), read on every call so tests can patch
`os.environ`; unset means off:

    ALERT_EMAILS          comma separated recipients
    ALERT_HEARTBEAT_URL   pinged with GET after every successful sweep

One mail per sweep at most, listing every fault and recovery together: a
TikTok layout change that hits every workspace at once is one mail, not nine.

**Conversations handed to a person** (`EscalationMailer`) are a third,
separate path. They are not faults but the shop's daily work ("Chờ tiếp quản"
on the dashboard), so they go to the people who do that work - the registered
email of every dashboard user granted the workspace - not to ALERT_EMAILS, and
much sooner: a customer is waiting. On by default when mail can be sent;
`ESCALATION_EMAILS=off` turns it off for the whole installation.
"""

import logging
import os
from datetime import datetime

import httpx

from dashboard_api import mailer, security_config


logger = logging.getLogger(__name__)

# Severities that page someone. `warn` stays on the dashboard: it covers
# per-conversation escalations, which are ordinary daily work.
ALERT_SEVERITIES = ("bad", "error")

# How long a fault must stay open before it is mailed. The monitor sweeps every
# 5 minutes, so a lasting fault is mailed 5-10 minutes after it starts.
ALERT_GRACE_SECONDS = 300

HEARTBEAT_TIMEOUT_SECONDS = 10

# A handed-over conversation is mailed once it has been open this long, checked
# this often - so 1-2 minutes after the agent gives up. Long enough that a
# person already at the dashboard who resolves it straight away is not mailed.
ESCALATION_GRACE_SECONDS = 60
ESCALATION_POLL_SECONDS = 60


def alert_recipients() -> list[str]:
    raw = os.environ.get("ALERT_EMAILS", "")
    return [item.strip() for item in raw.split(",") if "@" in item]


def _when(value) -> str:
    if not isinstance(value, datetime):
        return ""
    return value.astimezone().strftime("%H:%M %d/%m")


class AlertDispatcher:
    """Mails open serious notifications once, and their recovery once."""

    def __init__(self, notifications, *, should_alert=None, label_for=None,
                 send=None, grace_seconds: int = ALERT_GRACE_SECONDS):
        self.notifications = notifications
        # A fault that is not one - a workspace an operator stopped on purpose
        # still has its "agent not reachable" row - is held back here.
        self.should_alert = should_alert or (lambda row: True)
        self.label_for = label_for or (lambda account_id: account_id)
        # Looked up at call time so a test patching mailer.send is honoured.
        self._send = send
        self.grace_seconds = int(grace_seconds)
        self._unconfigured_logged = False

    def run_once(self) -> int:
        """Send at most one mail. Returns how many rows it reported."""
        recipients = alert_recipients()
        if not recipients:
            if not self._unconfigured_logged:
                logger.info("[alerts] ALERT_EMAILS not set; alerts stay on the dashboard")
                self._unconfigured_logged = True
            return 0
        self._unconfigured_logged = False

        opened = [
            row for row in self.notifications.pending_alerts(
                severities=ALERT_SEVERITIES, grace_seconds=self.grace_seconds,
            )
            if self.should_alert(row)
        ]
        recovered = self.notifications.pending_recoveries()
        if not opened and not recovered:
            return 0

        subject, body = self.compose(opened, recovered)
        send = self._send or mailer.send
        delivered = 0
        for recipient in recipients:
            try:
                send(recipient, subject, body)
                delivered += 1
            except Exception as exc:
                # The address stays out of the log, like mailer.send's own.
                logger.warning("[alerts] mail failed (%s)", type(exc).__name__, exc_info=True)
        if not delivered:
            # Nothing marked: the next sweep tries again rather than losing it.
            return 0
        self.notifications.mark_alerted([row["id"] for row in opened])
        self.notifications.mark_recovery_alerted([row["id"] for row in recovered])
        logger.warning(
            "[alerts] mailed %s fault(s) and %s recovery(ies) to %s recipient(s)",
            len(opened), len(recovered), delivered,
        )
        return len(opened) + len(recovered)

    def compose(self, opened, recovered) -> tuple[str, str]:
        if opened:
            first = f"{self.label_for(opened[0]['account_id'])}: {opened[0]['title']}"
            more = f" (+{len(opened) - 1} sự cố khác)" if len(opened) > 1 else ""
            subject = f"[Cảnh báo] {first}{more}"
        else:
            first = f"{self.label_for(recovered[0]['account_id'])}: {recovered[0]['title']}"
            more = f" (+{len(recovered) - 1})" if len(recovered) > 1 else ""
            subject = f"[Đã khắc phục] {first}{more}"

        lines = []
        if opened:
            lines.append(f"ĐANG LỖI ({len(opened)}):")
            for row in opened:
                lines.append(f"- [{self.label_for(row['account_id'])}] {row['title']}")
                if row.get("detail"):
                    lines.append(f"  {row['detail']}")
                lines.append(f"  Bắt đầu lúc {_when(row.get('opened_at'))}")
            lines.append("")
        if recovered:
            lines.append(f"ĐÃ KHẮC PHỤC ({len(recovered)}):")
            for row in recovered:
                lines.append(
                    f"- [{self.label_for(row['account_id'])}] {row['title']} "
                    f"(từ {_when(row.get('opened_at'))} đến {_when(row.get('resolved_at'))})"
                )
            lines.append("")
        url = os.environ.get("DASHBOARD_PUBLIC_URL", "").strip()
        if url:
            lines.append(f"Mở dashboard: {url}")
        lines.append(
            "Mail tự động từ health monitor. Mỗi sự cố chỉ được báo một lần khi xảy ra "
            "và một lần khi khắc phục."
        )
        return subject, "\n".join(lines)


def escalation_emails_enabled() -> bool:
    return os.environ.get("ESCALATION_EMAILS", "on").strip().lower() not in (
        "0", "off", "false", "no",
    )


class EscalationMailer:
    """Mails each handed-over conversation once to the workspace's own people.

    One mail per person per run, listing only workspaces that person has been
    granted: shop A's customers never appear in a mail to shop B's staff.
    """

    def __init__(self, notifications, *, recipients_for, label_for=None,
                 send=None, grace_seconds: int = ESCALATION_GRACE_SECONDS):
        self.notifications = notifications
        # account_ids -> {account_id: [email, ...]} (UserStore.workspace_emails).
        self.recipients_for = recipients_for
        self.label_for = label_for or (lambda account_id: account_id)
        self._send = send
        self.grace_seconds = int(grace_seconds)
        self._skip_logged = False

    def run_once(self) -> int:
        """Returns how many conversations were reported."""
        if not escalation_emails_enabled():
            return 0
        if not mailer.is_configured() and security_config.is_production():
            # mailer.send would refuse every time; say so once, not every minute.
            if not self._skip_logged:
                logger.warning("[alerts] SMTP_HOST not set; escalation mails are off")
                self._skip_logged = True
            return 0
        self._skip_logged = False

        rows = self.notifications.pending_escalations(grace_seconds=self.grace_seconds)
        if not rows:
            return 0
        recipients = self.recipients_for(sorted({row["account_id"] for row in rows}))
        by_email: dict[str, list[dict]] = {}
        for row in rows:
            for email in recipients.get(row["account_id"], []):
                by_email.setdefault(email, []).append(row)

        send = self._send or mailer.send
        delivered_ids: set[int] = set()
        for email, mine in by_email.items():
            subject, body = self.compose(mine)
            try:
                send(email, subject, body)
            except Exception as exc:
                # The address stays out of the log, like mailer.send's own.
                logger.warning(
                    "[alerts] escalation mail failed (%s)", type(exc).__name__, exc_info=True,
                )
                continue
            delivered_ids.update(row["id"] for row in mine)

        # A workspace nobody has been granted has no one to mail: marked all
        # the same, or it would be looked up again every minute forever. A
        # row whose every send failed stays unmarked and is tried next run.
        nobody = {row["id"] for row in rows if not recipients.get(row["account_id"])}
        self.notifications.mark_alerted(sorted(delivered_ids | nobody))
        if delivered_ids:
            logger.info(
                "[alerts] mailed %s handed-over conversation(s) to %s recipient(s)",
                len(delivered_ids), len(by_email),
            )
        return len(delivered_ids)

    def compose(self, rows) -> tuple[str, str]:
        first = rows[0]
        # Titles of known reasons already end in the customer's name.
        name = first.get("conversation") or ""
        what = first["title"] if not name or name in first["title"] else f"{first['title']}: {name}"
        more = f" (+{len(rows) - 1} hội thoại khác)" if len(rows) > 1 else ""
        subject = f"[Chờ tiếp quản] {self.label_for(first['account_id'])}: {what}{more}"

        lines = [f"Agent đã chuyển {len(rows)} hội thoại cho người xử lý:", ""]
        for row in rows:
            name = row.get("conversation") or ""
            lines.append(f"- [{self.label_for(row['account_id'])}] {row['title']}")
            if name and name not in row["title"]:
                lines.append(f"  Khách: {name}")
            if row.get("detail"):
                lines.append(f"  {row['detail']}")
            lines.append(f"  Lúc {_when(row.get('opened_at'))}")
        lines.append("")
        lines.append(
            "Khách trong các hội thoại này không được agent trả lời cho tới khi có "
            "người tiếp quản."
        )
        url = os.environ.get("DASHBOARD_PUBLIC_URL", "").strip()
        if url:
            lines.append(f"Mở dashboard: {url}")
        lines.append(
            "Mail tự động, gửi tới email tài khoản dashboard có quyền vào workspace. "
            "Mỗi hội thoại chỉ được báo một lần."
        )
        return subject, "\n".join(lines)


def ping_heartbeat() -> bool:
    """Tell the outside monitor this supervisor completed a sweep."""
    url = os.environ.get("ALERT_HEARTBEAT_URL", "").strip()
    if not url:
        return False
    try:
        httpx.get(url, timeout=HEARTBEAT_TIMEOUT_SECONDS).raise_for_status()
    except httpx.HTTPError as exc:
        # The URL itself carries the check's secret id: never logged.
        logger.warning("[alerts] heartbeat ping failed (%s)", type(exc).__name__)
        return False
    return True

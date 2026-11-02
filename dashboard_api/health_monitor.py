"""Periodic health checks that turn silence into a notification.

The gap this closes: the system already records everything needed to know it
is in trouble - the inbox ledger knows which conversations it cannot read,
`message_escalations` knows what was handed to a person, `tasks` knows what
failed - but nothing ever *looks*. Answering "did last night go badly?" meant
reading `journalctl` by eye.

Two deliberate choices:

* **Read-only, and nothing instruments the agent.** Every check below is a
  query against rows the agent already writes in the course of its normal
  work. No counter is added to a hot path, so a monitoring bug cannot slow or
  break a customer reply.
* **Notifications, not a metrics stack.** `notifications` already has dedup,
  severity, OPEN/RESOLVED and a place on the dashboard, so a check that fails
  raises exactly what an operator already knows how to read - no second
  system to run on a machine that is already short on memory.

A check reports a problem or reports nothing, and the monitor opens or
resolves one notification per check per account accordingly. Resolving is as
important as opening: an alert that never clears is one an operator learns to
ignore.
"""

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from time import time

from dashboard_api.alerts import ESCALATION_POLL_SECONDS, ping_heartbeat
from load_config import BASE_DIR


logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 300

# The inbox watcher keeps re-opening a thread it cannot read, on purpose, for
# up to MAX_TECHNICAL_RETRIES scans (platforms/tiktok/inbox_watcher.py). A few
# of those are an ordinary slow render. A conversation that has been retried
# this many times is not still rendering - and several at once is the shape a
# TikTok layout change makes, which is the single most valuable thing this
# module can notice early.
STUCK_ATTEMPTS = 8
STUCK_CONVERSATIONS = 3

OPEN_ESCALATIONS_WARN = 10

TASK_FAILURE_RATE_WARN = 0.5
TASK_FAILURE_MIN_SAMPLE = 10

# A customer whose message has had no answer for this long, in a conversation
# the agent still owns (AUTO), is the symptom every other check is a cause of:
# Chrome logged out, a scan loop stuck, a send that keeps failing. Messages are
# recorded when the agent reads the thread, so this cannot see a workspace that
# reads nothing at all - the hub's "agent not reachable" covers a dead agent.
# Only recent messages count: an old thread left unanswered on purpose (a
# duplicate the pipeline declined) must not page anyone forever.
CUSTOMER_WAIT_MINUTES = 15
CUSTOMER_WAIT_LOOKBACK_HOURS = 3

# A backup that quietly stopped running is worse than none, because it is
# trusted. The timer runs daily, so a day and a half without one is a fault.
BACKUP_MAX_AGE_HOURS = 36

# Free memory below which starting or waking one more Chrome risks the kernel
# killing one already serving customers. Either bound trips it: a small box
# runs out in absolute terms, a large one in relative terms.
MEMORY_LOW_MB = 768
MEMORY_LOW_FRACTION = 0.10


@dataclass(frozen=True)
class Problem:
    title: str
    detail: str
    severity: str = "warn"


class HealthMonitor:
    """Runs the checks below on a timer and mirrors them into notifications."""

    def __init__(self, notifications, database_url_resolver, list_accounts,
                 interval_seconds: int = CHECK_INTERVAL_SECONDS,
                 backup_dir: Path | None = None, alerts=None, is_open=None,
                 is_answering=None, escalation_mailer=None):
        self.notifications = notifications
        # dashboard_api/alerts.py: mails what these checks (and the hub) open.
        self.alerts = alerts
        # dashboard_api/alerts.py too, on its own faster timer: a customer
        # handed to a person should not wait for the next 5-minute sweep.
        self.escalation_mailer = escalation_mailer
        self._escalation_task: asyncio.Task | None = None
        # Is this workspace inside its operating hours? Outside them the agent
        # does not answer on purpose, so a waiting customer is not a fault.
        self._is_open = is_open or (lambda account_id: True)
        # Is this workspace expected to be answering right now? Not when an
        # operator stopped it or paused it (or it is asleep): a customer
        # waiting there is the operator's decision, not a fault to page about.
        self._is_answering = is_answering or (lambda account_id: True)
        self._resolve_dsn = database_url_resolver
        self._list_accounts = list_accounts
        self.interval = max(30, int(interval_seconds))
        self.backup_dir = backup_dir or (BASE_DIR / "data" / "backups")
        self.meminfo_path = "/proc/meminfo"
        self._task: asyncio.Task | None = None
        self.last_sweep_complete = False

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="health-monitor")
        if self.escalation_mailer is not None and self._escalation_task is None:
            self._escalation_task = asyncio.create_task(
                self._escalation_loop(), name="escalation-mailer",
            )

    async def stop(self) -> None:
        for task in (self._task, self._escalation_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._task = None
        self._escalation_task = None

    async def _escalation_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.escalation_mailer.run_once)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[health] escalation mail run failed")
            await asyncio.sleep(ESCALATION_POLL_SECONDS)

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.run_once)
                healthy = self.last_sweep_complete
                if self.alerts is not None:
                    await asyncio.to_thread(self.alerts.run_once)
                # Last, and only when the whole sweep worked: an outside monitor
                # that stops hearing from us is how a dead server, a dead
                # supervisor, an unreachable database or a broken alert path
                # gets reported at all.
                if healthy:
                    await asyncio.to_thread(ping_heartbeat)
            except asyncio.CancelledError:
                raise
            except Exception:
                # The monitor exists to report trouble; it must never become
                # trouble. A failed sweep is logged and the next one tries
                # again rather than taking the supervisor down with it.
                logger.exception("[health] check sweep failed")
            await asyncio.sleep(self.interval)

    # --------------------------------------------------------------- checks
    def run_once(self) -> dict[str, Problem | None]:
        """Run every check for every account. Returns what each one found."""
        found: dict[str, Problem | None] = {}
        accounts = [account.id for account in self._list_accounts()]
        # False when any check could not run - the heartbeat is then withheld,
        # since "every check passed" is what a ping claims.
        self.last_sweep_complete = True

        for account_id in accounts:
            for name, check in (
                ("scan_unreadable", self.check_unreadable_conversations),
                ("escalations_backlog", self.check_open_escalations),
                ("task_failures", self.check_task_failure_rate),
                ("customers_waiting", self.check_customers_waiting),
            ):
                try:
                    problem = check(account_id)
                except Exception:
                    logger.exception("[health] check=%s account=%s failed", name, account_id)
                    self.last_sweep_complete = False
                    continue
                found[f"{account_id}:{name}"] = problem
                self._publish(account_id, name, problem)

        # Not per-account: one backup and one machine cover the installation.
        for name, system_check in (
            ("backup", self.check_backup_freshness),
            ("backup_offsite", self.check_backup_offsite),
            ("memory", self.check_memory),
        ):
            try:
                problem = system_check()
            except Exception:
                logger.exception("[health] check=%s failed", name)
                self.last_sweep_complete = False
                continue
            found[name] = problem
            self._publish(accounts[0] if accounts else "system", name, problem)

        return found

    def check_unreadable_conversations(self, account_id: str) -> Problem | None:
        """Conversations the agent keeps opening but cannot read.

        This is the early-warning signal for a TikTok layout change: the
        watcher degrades quietly by design (it requeues rather than giving
        up), so without this the first symptom is a customer complaining.
        """
        rows = self._query(
            """SELECT conversation_name, attempts, status
                 FROM inbox_ledger
                WHERE account_id = %s
                  AND state = 'UNANSWERED'
                  AND attempts >= %s
             ORDER BY attempts DESC
                LIMIT 10""",
            (account_id, STUCK_ATTEMPTS),
            account_id=account_id,
        )
        if len(rows) < STUCK_CONVERSATIONS:
            return None
        worst = ", ".join(f"{name} ({attempts} lần)" for name, attempts, _ in rows[:3])
        return Problem(
            title=f"{len(rows)} hội thoại agent không đọc được",
            detail=(
                f"Đã thử lại nhiều lần mà vẫn không đọc được nội dung: {worst}. "
                "Nhiều hội thoại cùng kẹt thường có nghĩa TikTok vừa đổi giao diện - "
                "kiểm tra log agent tìm `bridge_unreadable`."
            ),
            severity="bad",
        )

    def check_customers_waiting(self, account_id: str) -> Problem | None:
        """Customers the agent still owns whose message has gone unanswered.

        Waiting starts at the first customer message after the shop's last
        reply (insertion order: a message is stored when the agent reads it).
        A conversation handed to a person (WAITING_HUMAN/HUMAN) is the
        escalation backlog's business, not this check's.
        """
        if not self._is_open(account_id) or not self._is_answering(account_id):
            return None
        rows = self._query(
            """WITH recent AS (
                   SELECT id, conversation_id, direction, created_at
                     FROM conversation_messages
                    WHERE account_id = %(account)s
                      AND created_at > now() - make_interval(hours => %(lookback)s::int)
               ), last_reply AS (
                   SELECT conversation_id, max(id) AS id
                     FROM recent WHERE direction = 'OUTBOUND'
                 GROUP BY conversation_id
               ), waiting AS (
                   SELECT recent.conversation_id, min(recent.created_at) AS since
                     FROM recent
                LEFT JOIN last_reply USING (conversation_id)
                    WHERE recent.direction = 'INBOUND'
                      AND recent.id > COALESCE(last_reply.id, 0)
                 GROUP BY recent.conversation_id
               )
               SELECT COALESCE(NULLIF(profile.display_name, ''),
                               NULLIF(profile.username, ''), waiting.conversation_id),
                      floor(extract(epoch FROM now() - waiting.since) / 60)::int
                 FROM waiting
            LEFT JOIN conversation_control AS control
                   ON control.account_id = %(account)s
                  AND control.conversation_id = waiting.conversation_id
            LEFT JOIN customer_profile AS profile
                   ON profile.account_id = %(account)s
                  AND profile.conversation_id = waiting.conversation_id
                WHERE waiting.since < now() - make_interval(mins => %(wait)s::int)
                  AND COALESCE(control.mode, 'AUTO') = 'AUTO'
             ORDER BY waiting.since
                LIMIT 10""",
            {
                "account": account_id,
                "lookback": CUSTOMER_WAIT_LOOKBACK_HOURS,
                "wait": CUSTOMER_WAIT_MINUTES,
            },
            account_id=account_id,
        )
        if not rows:
            return None
        worst = ", ".join(f"{name} ({minutes} phút)" for name, minutes in rows[:3])
        return Problem(
            title=f"{len(rows)} khách chờ quá {CUSTOMER_WAIT_MINUTES} phút chưa được trả lời",
            detail=(
                f"Agent đã đọc tin nhưng chưa trả lời: {worst}. Kiểm tra workspace "
                "còn đăng nhập TikTok, Chrome còn chạy, và log agent."
            ),
            severity="bad",
        )

    def check_open_escalations(self, account_id: str) -> Problem | None:
        rows = self._query(
            "SELECT count(*) FROM message_escalations "
            "WHERE account_id = %s AND status = 'OPEN'",
            (account_id,),
            account_id=account_id,
        )
        count = int(rows[0][0]) if rows else 0
        if count < OPEN_ESCALATIONS_WARN:
            return None
        return Problem(
            title=f"{count} hội thoại đang chờ người xử lý",
            detail=(
                "Số hội thoại agent chuyển cho người đã vượt ngưỡng. Khách trong "
                "các hội thoại này sẽ không được trả lời cho tới khi có người vào."
            ),
        )

    def check_task_failure_rate(self, account_id: str) -> Problem | None:
        rows = self._query(
            """SELECT
                 count(*) FILTER (WHERE status = 'failed'),
                 count(*) FILTER (WHERE status IN ('failed', 'completed'))
               FROM tasks
              WHERE account_id = %s
                AND updated_at > now() - interval '1 hour'""",
            (account_id,),
            account_id=account_id,
        )
        failed, total = (int(rows[0][0]), int(rows[0][1])) if rows else (0, 0)
        # A handful of tasks in an hour is too small a sample to call a rate;
        # one failure out of two is noise, not a trend.
        if total < TASK_FAILURE_MIN_SAMPLE:
            return None
        rate = failed / total
        if rate < TASK_FAILURE_RATE_WARN:
            return None
        return Problem(
            title=f"{failed}/{total} tác vụ thất bại trong 1 giờ qua",
            detail=(
                f"Tỷ lệ thất bại {rate:.0%} - cao bất thường. Thường là mất đăng nhập, "
                "Chrome không phản hồi, hoặc TikTok đổi giao diện."
            ),
            severity="bad",
        )

    def check_backup_freshness(self) -> Problem | None:
        archives = [
            *self.backup_dir.glob("backup-*.tar.gz"),
            *self.backup_dir.glob("backup-*.tar.gz.age"),
        ] if self.backup_dir.is_dir() else []
        if not archives:
            return Problem(
                title="Chưa có bản sao lưu nào",
                detail=(
                    "Không tìm thấy archive nào trong data/backups. Cài timer bằng "
                    "`sudo ./deploy/install_backup_timer.sh`."
                ),
                severity="bad",
            )
        newest = max(item.stat().st_mtime for item in archives)
        age_hours = (time() - newest) / 3600
        if age_hours <= BACKUP_MAX_AGE_HOURS:
            return None
        return Problem(
            title=f"Sao lưu đã cũ {age_hours:.0f} giờ",
            detail=(
                "Bản sao lưu gần nhất quá hạn so với lịch chạy hằng ngày. Kiểm tra: "
                "`journalctl -u autonomous-agent-backup -n 30`."
            ),
            severity="bad",
        )

    def check_backup_offsite(self) -> Problem | None:
        """What the last deploy/backup.sh run said about itself.

        The freshness check above only sees the local archive. A run that
        wrote it but could not copy it offsite - credentials expired, the
        remote full, plaintext refused - leaves a fresh local file and a
        disaster-recovery plan that silently stopped working.
        """
        status_path = self.backup_dir / "last-run.json"
        if not status_path.exists():
            return None  # no run yet: the freshness check already says so
        status = json.loads(status_path.read_text(encoding="utf-8"))
        error = str(status.get("error") or "")
        remote = status.get("remote")
        if remote in ("failed", "refused_plaintext"):
            return Problem(
                title="Sao lưu không đẩy được ra ngoài máy",
                detail=(
                    f"{error or remote}. Bản sao lưu chỉ còn trên ổ đĩa này - mất máy "
                    "là mất cả dữ liệu lẫn bản sao lưu. Kiểm tra: "
                    "`journalctl -u autonomous-agent-backup -n 30`."
                ),
                severity="bad",
            )
        if not status.get("ok"):
            return Problem(
                title="Lần sao lưu gần nhất thất bại",
                detail=f"{error}. Kiểm tra: `journalctl -u autonomous-agent-backup -n 30`.",
                severity="bad",
            )
        if remote == "disabled":
            return Problem(
                title="Sao lưu chỉ nằm trên máy này",
                detail=(
                    "BACKUP_REMOTE chưa đặt: hỏng ổ đĩa hay mất máy là mất cả bản sao "
                    "lưu. Xem deploy/README.md mục 7."
                ),
                severity="warn",
            )
        return None

    def check_memory(self) -> Problem | None:
        total = available = None
        for line in Path(self.meminfo_path).read_text(encoding="utf-8").splitlines():
            name, _, rest = line.partition(":")
            if name == "MemTotal":
                total = int(rest.split()[0]) // 1024
            elif name == "MemAvailable":
                available = int(rest.split()[0]) // 1024
        if not total or available is None:
            return None
        if available >= MEMORY_LOW_MB and available >= total * MEMORY_LOW_FRACTION:
            return None
        return Problem(
            title=f"RAM còn trống thấp: {available} MB / {total} MB",
            detail=(
                "Thêm một Chrome (Start hoặc đánh thức workspace) có thể khiến hệ điều "
                "hành kill một workspace đang trả lời khách. Dừng bớt workspace không "
                "dùng, hoặc nâng RAM - xem deploy/README.md mục 'Xem RAM đang dùng'."
            ),
            severity="bad",
        )

    # --------------------------------------------------------------- output
    def _publish(self, account_id: str, kind: str, problem: Problem | None) -> None:
        """Open the notification for a failing check, clear it when it passes."""
        try:
            if problem is None:
                self.notifications.resolve_kind(account_id, "health_check", kind)
                return
            self.notifications.record(
                account_id=account_id, kind="health_check", dedup_key=kind,
                severity=problem.severity, title=problem.title, detail=problem.detail,
            )
        except Exception:
            logger.exception("[health] publishing check=%s failed", kind)

    def _query(self, sql: str, params, *, account_id: str) -> list[tuple]:
        """Run one check's query for one workspace, row-level security on:
        the monitor reads every shop, but each query only ever sees one."""
        import psycopg

        from storage.tenancy import scope_to_account

        with psycopg.connect(self._resolve_dsn(), autocommit=True) as conn:
            scope_to_account(conn, account_id)
            return conn.execute(sql, params).fetchall()

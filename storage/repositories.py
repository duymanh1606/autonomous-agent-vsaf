import json
import logging
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from models.account import assert_account_scope, require_account_id
from models.message_delivery import MessageSendState, SendClaimOutcome, SendTaskState
from models.tasks import Task, TaskClaim, TaskClaimState
from storage.conversation_context import ConversationContextRepository

logger = logging.getLogger(__name__)


DEFAULT_IN_FLIGHT_STALE_SECONDS = 180
DEFAULT_TASK_LEASE_SECONDS = 180


def _now():
    return datetime.now(timezone.utc)


def _parse_time(value):
    if not value:
        return None
    text = str(value).strip().replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class SendClaim:
    """Answer of the dedup layer to "may this reply be sent now?".

    A refused claim is never proof that the customer received anything; the
    caller must re-read the open conversation before concluding that.
    """

    granted: bool
    state: str
    reason: str
    task_id: str = ""
    attempt_count: int = 0
    signature: str = ""
    outcome: str = SendClaimOutcome.GRANTED.value

    @property
    def already_sent(self) -> bool:
        return self.state == SendTaskState.SENT.value

    @property
    def in_flight(self) -> bool:
        return not self.granted and self.state == SendTaskState.IN_FLIGHT.value


class TaskRepository:
    """Tasks, send-dedup and outbound rows for exactly one account.

    Bound to an account at construction and injecting `account_id` into every
    statement, so no caller can write a query that forgets the scope. A task
    arriving from outside is checked against that scope before it is stored.
    """

    def __init__(
        self, db, *, account_id=None, owner_id=None,
        task_lease_seconds=DEFAULT_TASK_LEASE_SECONDS,
        send_lease_seconds=DEFAULT_IN_FLIGHT_STALE_SECONDS,
    ):
        resolved = account_id if account_id is not None else getattr(db, "account_id", None)
        self.account_id = require_account_id(resolved)
        self.db = db
        self.owner_id = str(owner_id or (
            f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:12]}"
        ))
        self.task_lease_seconds = max(10, int(task_lease_seconds))
        self.send_lease_seconds = max(10, int(send_lease_seconds))

    def _check(self, task, boundary):
        """Refuse a task belonging to another account."""
        assert_account_scope(
            self.account_id, getattr(task, "account_id", None), boundary=boundary
        )

    def requeue_interrupted_tasks(self):
        """Requeue every 'running' task for this account: called once at boot,
        before this process has claimed anything, so any such row can only be
        left over from a previous process for the same account - one worker
        serves exactly one account, so there is no live owner to race with.

        Filtering by `lease_expires_at <= now` here would defeat the point:
        a task whose lease had not yet elapsed would stay stuck at 'running'
        with no process left to finish it, for up to `task_lease_seconds`
        after every restart.
        """
        now = _now().isoformat()
        updated = self.db.conn.execute(
            "UPDATE tasks SET status = 'pending', owner_id = NULL, claimed_at = NULL, "
            "lease_expires_at = NULL, updated_at = %s "
            "WHERE account_id = %s AND status = 'running'",
            (now, self.account_id),
        )
        self.db.conn.commit()
        return updated.rowcount

    def expire_interrupted_send_tasks(self):
        """A restart means nothing is in flight any more.

        The rows stay as evidence, but as `EXPIRED` they allow a retry instead
        of blocking one forever - the conversation still decides whether the
        retry actually sends anything. Same reasoning as
        `requeue_interrupted_tasks`: called once at boot for this account
        only, so every still-`IN_FLIGHT` row here is left over from a dead
        process, whether or not its lease had technically elapsed yet.
        """
        now = _now().isoformat()
        updated = self.db.conn.execute(
            "UPDATE send_tasks SET state = %s, failure_reason = %s, updated_at = %s "
            "WHERE account_id = %s AND state = %s",
            (
                SendTaskState.EXPIRED.value, "worker_interrupted", now,
                self.account_id, SendTaskState.IN_FLIGHT.value,
            ),
        )
        self.db.conn.commit()
        return updated.rowcount

    async def has_active_capability(self, capability):
        row = self.db.conn.execute(
            "SELECT 1 FROM tasks "
            "WHERE account_id = %s AND capability = %s AND status IN ('pending', 'running') "
            "LIMIT 1",
            (self.account_id, capability),
        ).fetchone()
        return row is not None

    async def has_pending_above_priority(self, priority):
        """True when work more urgent than `priority` is queued and waiting.

        The agent owns one Chrome session and runs one task at a time, so a
        long-running task has to be able to ask whether it is standing in
        somebody's way. That somebody is a staff reply: `dashboard_api/main.py`
        submits those at priority 100, far above the 5 every routine task
        uses, precisely so they are claimed the instant the current task ends
        - which is no help at all while an inbox scan is holding a
        conversation for its full quiet period.

        Covered by `idx_tasks_claim_ready` (account_id, priority DESC, ...).
        """
        row = self.db.conn.execute(
            "SELECT 1 FROM tasks "
            "WHERE account_id = %s AND status = 'pending' AND priority > %s "
            "LIMIT 1",
            (self.account_id, int(priority)),
        ).fetchone()
        return row is not None

    async def prune_completed_capability(self, capability, keep=100):
        keep = max(1, int(keep))
        rows = self.db.conn.execute(
            "SELECT id FROM tasks "
            "WHERE account_id = %s AND capability = %s AND status IN ('completed', 'failed') "
            "ORDER BY created_at DESC OFFSET %s",
            (self.account_id, capability, keep),
        ).fetchall()
        task_ids = [row["id"] for row in rows]
        if not task_ids:
            return 0
        placeholders = ",".join("%s" for _ in task_ids)
        scope = (self.account_id, *task_ids)
        self.db.conn.execute(
            f"DELETE FROM actions WHERE account_id = %s AND task_id IN ({placeholders})",
            scope,
        )
        self.db.conn.execute(
            f"UPDATE message_events SET reply_task_id = NULL WHERE account_id = %s "
            f"AND reply_task_id IN ({placeholders})",
            scope,
        )
        deleted = self.db.conn.execute(
            f"DELETE FROM tasks WHERE account_id = %s AND id IN ({placeholders})", scope
        )
        self.db.conn.commit()
        return deleted.rowcount

    @staticmethod
    def _task_from_row(row):
        return Task(
            id=row["id"], account_id=row["account_id"], capability=row["capability"],
            platform=row["platform"], priority=row["priority"],
            payload=json.loads(row["payload"] or "{}"), status="running",
        )

    async def claim_next_task(self, lease_seconds=None, *, human_only=False):
        """Atomically claim one queued task with a short database transaction.

        A PostgreSQL transaction-scoped advisory lock serializes claims for one
        account/capability. It is released at commit, before any browser work.
        This prevents two pending scan rows from becoming running concurrently.
        """
        lease = max(10, int(lease_seconds or self.task_lease_seconds))
        now = _now()
        expires = now + timedelta(seconds=lease)
        with self.db.conn.transaction():
            # human_only: what a paused or closed workspace still runs - a
            # message a person typed in the dashboard (core/graph.py's run).
            candidate = self.db.conn.execute(
                "SELECT * FROM tasks WHERE account_id = %s AND ("
                "status = 'pending' OR (status = 'running' AND lease_expires_at <= %s)) "
                + ("AND payload::jsonb ->> 'origin' = 'human' " if human_only else "")
                + "ORDER BY priority DESC, created_at ASC FOR UPDATE SKIP LOCKED LIMIT 1",
                (self.account_id, now.isoformat()),
            ).fetchone()
            if candidate is None:
                return None
            capability = str(candidate["capability"] or "")
            self.db.conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                (self.account_id, capability),
            )
            active = self.db.conn.execute(
                "SELECT id, owner_id FROM tasks WHERE account_id = %s "
                "AND capability = %s AND status = 'running' "
                "AND lease_expires_at > %s AND id <> %s LIMIT 1",
                (self.account_id, capability, now.isoformat(), candidate["id"]),
            ).fetchone()
            if active is not None:
                return TaskClaim(
                    TaskClaimState.LOCKED, reason="capability_locked",
                    owner_id=str(active["owner_id"] or ""),
                )
            expired = str(candidate["status"]) == "running"
            row = self.db.conn.execute(
                "UPDATE tasks SET status = 'running', owner_id = %s, claimed_at = %s, "
                "lease_expires_at = %s, attempt_count = COALESCE(attempt_count, 0) + 1, "
                "updated_at = %s WHERE account_id = %s AND id = %s RETURNING *",
                (
                    self.owner_id, now.isoformat(), expires.isoformat(), now.isoformat(),
                    self.account_id, candidate["id"],
                ),
            ).fetchone()
        return TaskClaim(
            TaskClaimState.EXPIRED if expired else TaskClaimState.GRANTED,
            task=self._task_from_row(row),
            reason="expired_lease_recovered" if expired else "claimed",
            owner_id=self.owner_id,
            attempt_count=int(row["attempt_count"] or 0),
        )

    async def claim_task(self, task_id, lease_seconds=None):
        """Claim a specific task and expose all four required claim outcomes."""
        lease = max(10, int(lease_seconds or self.task_lease_seconds))
        now = _now()
        with self.db.conn.transaction():
            row = self.db.conn.execute(
                "SELECT * FROM tasks WHERE account_id = %s AND id = %s FOR UPDATE",
                (self.account_id, task_id),
            ).fetchone()
            if row is None or str(row["status"]) in {"completed", "failed"}:
                return TaskClaim(TaskClaimState.DUPLICATE, reason="terminal_or_missing")
            lease_expires = _parse_time(row["lease_expires_at"])
            if str(row["status"]) == "running" and lease_expires and lease_expires > now:
                if str(row["owner_id"] or "") != self.owner_id:
                    return TaskClaim(
                        TaskClaimState.LOCKED, reason="owned_by_other",
                        owner_id=str(row["owner_id"] or ""),
                        attempt_count=int(row["attempt_count"] or 0),
                    )
                return TaskClaim(
                    TaskClaimState.GRANTED, task=self._task_from_row(row),
                    reason="resumed_own_claim", owner_id=self.owner_id,
                    attempt_count=int(row["attempt_count"] or 0),
                )
            expired = str(row["status"]) == "running"
            expires = now + timedelta(seconds=lease)
            claimed = self.db.conn.execute(
                "UPDATE tasks SET status = 'running', owner_id = %s, claimed_at = %s, "
                "lease_expires_at = %s, attempt_count = COALESCE(attempt_count, 0) + 1, "
                "updated_at = %s WHERE account_id = %s AND id = %s RETURNING *",
                (
                    self.owner_id, now.isoformat(), expires.isoformat(), now.isoformat(),
                    self.account_id, task_id,
                ),
            ).fetchone()
        return TaskClaim(
            TaskClaimState.EXPIRED if expired else TaskClaimState.GRANTED,
            task=self._task_from_row(claimed),
            reason="expired_lease_recovered" if expired else "claimed",
            owner_id=self.owner_id,
            attempt_count=int(claimed["attempt_count"] or 0),
        )

    async def get_next_task(self, *, human_only=False):
        claim = await self.claim_next_task(human_only=human_only)
        return claim.task if claim and claim.granted else None

    async def renew_task_lease(self, task_id, lease_seconds=None):
        lease = max(10, int(lease_seconds or self.task_lease_seconds))
        now = _now()
        updated = self.db.conn.execute(
            "UPDATE tasks SET lease_expires_at = %s, updated_at = %s "
            "WHERE account_id = %s AND id = %s AND status = 'running' AND owner_id = %s",
            (
                (now + timedelta(seconds=lease)).isoformat(), now.isoformat(),
                self.account_id, task_id, self.owner_id,
            ),
        )
        self.db.conn.commit()
        return updated.rowcount == 1

    async def submit(self, task):
        self._check(task, "TaskRepository.submit")
        self.db.conn.execute(
            "INSERT INTO tasks(id, account_id, capability, platform, priority, payload, status, "
            "owner_id, claimed_at, lease_expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, "
            "CASE WHEN %s = 'running' THEN CURRENT_TIMESTAMP END, "
            "CASE WHEN %s = 'running' THEN CURRENT_TIMESTAMP + (%s * INTERVAL '1 second') END)",
            (
                task.id, self.account_id, task.capability, task.platform,
                task.priority, json.dumps(task.payload), task.status,
                self.owner_id if task.status == "running" else None,
                task.status, task.status, self.task_lease_seconds,
            ),
        )
        self.db.conn.commit()

    async def complete(self, task_id, success, error=None, result=None, *, task=None):
        status = "completed" if success else "failed"
        with self.db.conn.transaction():
            self.db.conn.execute(
                "UPDATE tasks SET status = %s, error = %s, result = %s, owner_id = NULL, "
                "lease_expires_at = NULL, updated_at = CURRENT_TIMESTAMP "
                "WHERE account_id = %s AND id = %s",
                (
                    status, error, json.dumps(result or {}, ensure_ascii=False),
                    self.account_id, task_id,
                ),
            )
            outbound = self.db.conn.execute(
                "SELECT * FROM outbound_messages WHERE account_id = %s AND id = %s",
                (self.account_id, task_id),
            ).fetchone()
            if outbound is None and success and task is not None and (
                task.capability in {"send_message", "reply_to_message"}
                and str(task.payload.get("origin") or "") == "human"
            ):
                # A dashboard-submitted send has no `outbound_messages` row
                # (only the since-removed queue-driven reply path ever wrote
                # that table), so the cascade above never
                # fires for it. Write the OUTBOUND row directly from the
                # task's own payload instead - this is the only persistence
                # a manual send gets, so without it a verified TikTok send
                # would never appear in the dashboard.
                payload = task.payload
                message_id = str(
                    payload.get("client_message_id") or task_id
                )
                # A file the operator attached goes into the history as well,
                # in the same {kind, path, name} shape an agent reply's media
                # has, so the dashboard shows the picture that was sent.
                media = [
                    {
                        "kind": str(item.get("kind") or "image"),
                        "path": str(item.get("path") or ""),
                        "name": str(item.get("name") or ""),
                    }
                    for item in (payload.get("attachments") or ())
                    if isinstance(item, dict) and item.get("path")
                ]
                self.db.conn.execute(
                    "INSERT INTO conversation_messages("
                    "account_id, message_id, conversation_id, customer_id, direction, "
                    "text, created_at, source, media_json) "
                    "VALUES (%s, %s, %s, %s, 'OUTBOUND', %s, %s, 'human', %s) "
                    "ON CONFLICT DO NOTHING",
                    (
                        self.account_id, message_id,
                        str(payload.get("conversation_id") or ""),
                        str(payload.get("conversation_id") or ""),
                        str(payload.get("response") or ""),
                        _now().isoformat(),
                        json.dumps(media, ensure_ascii=False) if media else None,
                    ),
                )
            elif outbound is None and success and task is not None and (
                task.capability == "send_message"
                and task.payload.get("automatic") is True
                and str(task.payload.get("origin") or "") == "agent"
            ):
                # A follow-up nudge (messaging/followup_scheduler.py) runs as
                # its own send task, outside the inbox scan whose
                # `record_sent` writes every other agent reply down - so,
                # like a manual send, this was its only chance to be stored.
                # Hashed with no DOM position, exactly as `record_sent`
                # stores a reply, so the next re-read of the thread re-keys
                # this row (`_promote_sent_row`) instead of adding a second.
                self._record_automatic_send(task.payload)
            if outbound:
                outbound_status = "SENT" if success else "FAILED"
                self.db.conn.execute(
                    "UPDATE outbound_messages SET status = %s, completed_at = CURRENT_TIMESTAMP "
                    "WHERE account_id = %s AND id = %s",
                    (outbound_status, self.account_id, task_id),
                )
                if success:
                    self.db.conn.execute(
                        "INSERT INTO conversation_messages("
                        "account_id, message_id, conversation_id, customer_id, direction, text, "
                        "created_at) VALUES (%s, %s, %s, %s, 'OUTBOUND', %s, CURRENT_TIMESTAMP) "
                        "ON CONFLICT DO NOTHING",
                        (
                            self.account_id, outbound["id"], outbound["conversation_id"],
                            outbound["customer_id"], outbound["text"],
                        ),
                    )
                    self.db.conn.execute(
                        "UPDATE message_events SET status = 'REPLIED', "
                        "processed_at = CURRENT_TIMESTAMP WHERE account_id = %s AND event_id = %s",
                        (self.account_id, outbound["source_event_id"]),
                    )
                else:
                    self.db.conn.execute(
                        "UPDATE message_events SET status = 'FAILED', error = %s, "
                        "processed_at = CURRENT_TIMESTAMP WHERE account_id = %s AND event_id = %s",
                        (error or "outbound_task_failed", self.account_id, outbound["source_event_id"]),
                    )
                    state = self.db.conn.execute(
                        "SELECT state_json FROM customer_states WHERE account_id = %s "
                        "AND conversation_id = %s",
                        (self.account_id, outbound["conversation_id"]),
                    ).fetchone()
                    self.db.conn.execute(
                        "INSERT INTO message_escalations("
                        "account_id, event_id, conversation_id, customer_id, reason, state_json) "
                        "VALUES (%s, %s, %s, %s, 'outbound_task_failed', %s) "
                        "ON CONFLICT DO NOTHING",
                        (
                            self.account_id, outbound["source_event_id"],
                            outbound["conversation_id"], outbound["customer_id"],
                            state["state_json"] if state else "{}",
                        ),
                    )

    def _record_automatic_send(self, payload):
        """Store a verified automatic send the way `record_sent` stores a reply."""
        conversation_id = str(payload.get("conversation_id") or "")
        text = str(payload.get("response") or "").strip()
        if not conversation_id or not text:
            return
        digest = ConversationContextRepository.message_hash(
            conversation_id, "OUTBOUND", text, self.account_id, "",
        )
        # A savepoint: a history row that cannot be written must not roll
        # back the task's own completion around it.
        try:
            with self.db.conn.transaction():
                self.db.conn.execute(
                    "INSERT INTO conversation_messages("
                    "account_id, message_id, conversation_id, customer_id, direction, "
                    "text, created_at, source, message_hash) "
                    "VALUES (%s, %s, %s, %s, 'OUTBOUND', %s, %s, 'dom', %s) "
                    "ON CONFLICT DO NOTHING",
                    (
                        self.account_id, f"dom-{digest[:32]}", conversation_id,
                        conversation_id, text, _now().isoformat(), digest,
                    ),
                )
        except Exception:
            logger.exception(
                "[DB] account=%s storing an automatic send failed conversation_id=%s",
                self.account_id, conversation_id,
            )

    async def mark_done(self, task_id):
        await self.complete(task_id, True)

    async def record_action(self, task_id, action, result):
        self.db.conn.execute(
            "INSERT INTO actions("
            "account_id, task_id, action_name, args, success, error) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                self.account_id, task_id, action.name, json.dumps(action.args),
                int(result.success), result.error,
            ),
        )
        self.db.conn.commit()

    async def record_transition(self, before, action_name, after, confidence):
        confidence = min(1.0, max(0.0, float(confidence)))
        self.db.conn.execute(
            "INSERT INTO screen_transition_stats("
            "account_id, from_screen, action_name, to_screen, average_confidence, "
            "max_confidence, last_confidence) VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT(account_id, from_screen, action_name, to_screen) DO UPDATE SET "
            "average_confidence = (screen_transition_stats.average_confidence * "
            "screen_transition_stats.observation_count + excluded.last_confidence) / "
            "(screen_transition_stats.observation_count + 1), "
            "max_confidence = GREATEST(screen_transition_stats.max_confidence, "
            "excluded.last_confidence), last_confidence = excluded.last_confidence, "
            "observation_count = screen_transition_stats.observation_count + 1, "
            "last_seen = CURRENT_TIMESTAMP",
            (self.account_id, before, action_name, after, confidence, confidence, confidence),
        )
        self.db.conn.commit()

    async def get_send_state(self, task_id):
        row = self.db.conn.execute(
            "SELECT send_status, send_attempt_count, message_signature, "
            "last_verified_at, verification_source FROM tasks "
            "WHERE account_id = %s AND id = %s",
            (self.account_id, task_id),
        ).fetchone()
        return dict(row) if row else None

    async def ensure_task(self, task):
        """Persist a task row if it does not exist yet.

        Replies generated inside an inbox scan are real send tasks; without a
        row their send state cannot be recorded, which is exactly how a send
        used to be rejected as a "signature conflict".
        """
        # A task created inside a running flow is already being executed, so it
        # is never stored as `pending`: the scheduler must not pick it up again.
        status = "running" if task.status == "pending" else task.status
        self._check(task, "TaskRepository.ensure_task")
        self.db.conn.execute(
            "INSERT INTO tasks("
            "id, account_id, capability, platform, priority, payload, status, owner_id, "
            "claimed_at, lease_expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, "
            "CASE WHEN %s = 'running' THEN CURRENT_TIMESTAMP END, "
            "CASE WHEN %s = 'running' THEN CURRENT_TIMESTAMP + (%s * INTERVAL '1 second') END) "
            "ON CONFLICT(account_id, id) DO NOTHING",
            (
                task.id, self.account_id, task.capability, task.platform,
                task.priority, json.dumps(task.payload, ensure_ascii=False), status,
                self.owner_id if status == "running" else None,
                status, status, self.task_lease_seconds,
            ),
        )
        self.db.conn.commit()

    # ------------------------------------------------------- send dedup layer
    async def get_send_task(self, signature):
        row = self.db.conn.execute(
            "SELECT * FROM send_tasks WHERE account_id = %s AND signature = %s",
            (self.account_id, signature),
        ).fetchone()
        return dict(row) if row else None

    async def claim_send_task(
        self, *, signature, task_id, platform="", conversation_id="",
        inbound_message_id="", inbound_watermark="", reply_hash="",
        attachment_hashes=(), lease_seconds=None, stale_after_seconds=None,
    ):
        """Claim one send atomically; expired claims require DOM reconciliation."""
        now = _now()
        # `stale_after_seconds` remains a compatibility override for callers
        # and tests written before explicit send leases existed.
        lease = max(0, int(
            stale_after_seconds if stale_after_seconds is not None
            else (lease_seconds if lease_seconds is not None else self.send_lease_seconds)
        ))
        expires = now + timedelta(seconds=lease)
        hashes_json = json.dumps(sorted(set(attachment_hashes or ())))
        with self.db.conn.transaction():
            # Some UI reply paths start at the send guard rather than the
            # scheduler. Materialize that work item before the composite FK is
            # used; a later ensure_task() sees the same account-scoped id.
            self.db.conn.execute(
                "INSERT INTO tasks(account_id, id, capability, platform, payload, status, "
                "owner_id, claimed_at, lease_expires_at) VALUES (%s, %s, 'send_message', "
                "%s, '{}', 'running', %s, %s, %s) "
                "ON CONFLICT(account_id, id) DO NOTHING",
                (
                    self.account_id, task_id, platform or "tiktok_web", self.owner_id,
                    now.isoformat(), expires.isoformat(),
                ),
            )
            inserted = self.db.conn.execute(
                "INSERT INTO send_tasks(account_id, signature, task_id, platform, "
                "conversation_id, inbound_message_id, inbound_watermark, reply_hash, "
                "text_fingerprint, attachment_hashes_json, owner_id, state, attempt_count, "
                "claimed_at, lease_expires_at, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s, %s, %s) "
                "ON CONFLICT(account_id, signature) DO NOTHING RETURNING *",
                (
                    self.account_id, signature, task_id, platform, conversation_id,
                    inbound_message_id, inbound_watermark, reply_hash, reply_hash,
                    hashes_json, self.owner_id, SendTaskState.IN_FLIGHT.value,
                    now.isoformat(), expires.isoformat(), now.isoformat(), now.isoformat(),
                ),
            ).fetchone()
            if inserted is not None:
                return SendClaim(
                    granted=True, state=SendTaskState.IN_FLIGHT.value, reason="claimed",
                    task_id=task_id, attempt_count=1, signature=signature,
                    outcome=SendClaimOutcome.GRANTED.value,
                )

        # The conflict row is inspected and, when safe, updated under one row
        # lock. The lock is released before any browser observation begins.
        with self.db.conn.transaction():
            row = self.db.conn.execute(
                "SELECT * FROM send_tasks WHERE account_id = %s AND signature = %s "
                "FOR UPDATE",
                (self.account_id, signature),
            ).fetchone()
            state = str(row["state"] or SendTaskState.IN_FLIGHT.value)
            attempts = int(row["attempt_count"] or 0)
            if state == SendTaskState.SENT.value:
                return SendClaim(
                    granted=False, state=state, reason="already_sent",
                    task_id=str(row["task_id"] or ""), attempt_count=attempts,
                    signature=signature, outcome=SendClaimOutcome.ALREADY_SENT.value,
                )
            if state == SendTaskState.IN_FLIGHT.value:
                updated_at = _parse_time(row["updated_at"]) or now
                if stale_after_seconds is not None:
                    # An explicit override always judges staleness against
                    # this row's own age, ignoring its stored lease - the
                    # caller is asking "is this older than N seconds?", not
                    # "has its own lease elapsed?".
                    stale = now - updated_at >= timedelta(
                        seconds=max(0, int(stale_after_seconds))
                    )
                else:
                    lease_expires = _parse_time(row["lease_expires_at"])
                    stale = (
                        lease_expires <= now if lease_expires is not None
                        else now - updated_at >= timedelta(seconds=lease)
                    )
                if str(row["task_id"] or "") == task_id and not stale:
                    return SendClaim(
                        granted=True, state=state, reason="resumed_own_attempt",
                        task_id=task_id, attempt_count=attempts, signature=signature,
                        outcome=SendClaimOutcome.GRANTED.value,
                    )
                if not stale:
                    return SendClaim(
                        granted=False, state=state, reason="in_flight",
                        task_id=str(row["task_id"] or ""), attempt_count=attempts,
                        signature=signature,
                        outcome=SendClaimOutcome.IN_FLIGHT_BY_OTHER.value,
                    )
                self.db.conn.execute(
                    "UPDATE send_tasks SET state = %s, failure_reason = %s, updated_at = %s "
                    "WHERE account_id = %s AND signature = %s",
                    (
                        SendTaskState.EXPIRED.value,
                        "lease_expired_requires_reconciliation", now.isoformat(),
                        self.account_id, signature,
                    ),
                )
                return SendClaim(
                    granted=False, state=SendTaskState.EXPIRED.value,
                    reason="expired_requires_reconciliation",
                    task_id=str(row["task_id"] or ""), attempt_count=attempts,
                    signature=signature,
                    outcome=SendClaimOutcome.EXPIRED_REQUIRES_RECONCILIATION.value,
                )
            if state == SendTaskState.EXPIRED.value:
                return SendClaim(
                    granted=False, state=state,
                    reason="expired_requires_reconciliation",
                    task_id=str(row["task_id"] or ""), attempt_count=attempts,
                    signature=signature,
                    outcome=SendClaimOutcome.EXPIRED_REQUIRES_RECONCILIATION.value,
                )
            # FAILED/ABORTED represent failures before a send click or a result
            # explicitly proved NOT_SENT, and are safe to claim again.
            self.db.conn.execute(
                "UPDATE send_tasks SET task_id = %s, state = %s, "
                "attempt_count = attempt_count + 1, error = NULL, failure_reason = NULL, "
                "owner_id = %s, claimed_at = %s, lease_expires_at = %s, updated_at = %s "
                "WHERE account_id = %s AND signature = %s",
                (
                    task_id, SendTaskState.IN_FLIGHT.value, self.owner_id,
                    now.isoformat(), expires.isoformat(), now.isoformat(),
                    self.account_id, signature,
                ),
            )
            return SendClaim(
                granted=True, state=SendTaskState.IN_FLIGHT.value,
                reason=f"retry_after_{state.casefold()}", task_id=task_id,
                attempt_count=attempts + 1, signature=signature,
                outcome=SendClaimOutcome.GRANTED.value,
            )

    async def mark_send_task(self, signature, state, task_id=None, error=None):
        """Record the outcome of one claimed send attempt."""
        value = state.value if isinstance(state, SendTaskState) else str(state)
        updated = self.db.conn.execute(
            "UPDATE send_tasks SET state = %s, error = %s, failure_reason = %s, "
            "verified_at = CASE WHEN %s = %s THEN %s ELSE verified_at END, "
            "lease_expires_at = NULL, updated_at = %s "
            "WHERE account_id = %s AND signature = %s"
            + (" AND task_id = %s" if task_id else ""),
            (
                (
                    value, error, error, value, SendTaskState.SENT.value,
                    _now().isoformat(), _now().isoformat(), self.account_id, signature,
                )
                + ((task_id,) if task_id else ())
            ),
        )
        self.db.conn.commit()
        return updated.rowcount == 1

    async def expire_stale_send_tasks(
        self, stale_after_seconds=DEFAULT_IN_FLIGHT_STALE_SECONDS
    ):
        """Free `IN_FLIGHT` rows whose owner disappeared (crash, restart)."""
        cutoff = _now() - timedelta(seconds=max(0, int(stale_after_seconds)))
        now = _now().isoformat()
        updated = self.db.conn.execute(
            "UPDATE send_tasks SET state = %s, failure_reason = %s, updated_at = %s "
            "WHERE account_id = %s AND state = %s AND ("
            "lease_expires_at <= %s OR (lease_expires_at IS NULL AND updated_at < %s))",
            (
                SendTaskState.EXPIRED.value, "lease_expired_requires_reconciliation",
                now, self.account_id, SendTaskState.IN_FLIGHT.value,
                now, cutoff.isoformat(),
            ),
        )
        self.db.conn.commit()
        return updated.rowcount

    async def bind_message_signature(self, task_id, signature):
        """Bind the reply this task is about to send.

        Rebinding is allowed while no send attempt has been made yet - a reply
        regenerated after a failed attempt is a different signature, and
        refusing it here is what used to strand unanswered messages. Once an
        attempt exists the binding is frozen, so a send can never be swapped
        for different text mid-flight.
        """
        try:
            updated = self.db.conn.execute(
                "UPDATE tasks SET message_signature = %s "
                "WHERE account_id = %s AND id = %s "
                "AND (message_signature IS NULL OR message_signature = %s "
                "OR COALESCE(send_attempt_count, 0) = 0)",
                (signature, self.account_id, task_id, signature),
            )
            self.db.conn.commit()
        except Exception:
            # Refusing the binding refuses the send (fail-closed) - but a
            # database error must not pass for an ordinary conflict in the log.
            logger.warning("[tasks] binding message signature failed task_id=%s",
                           task_id, exc_info=True)
            self.db.conn.rollback()
            return False
        return updated.rowcount == 1

    async def reserve_send_attempt(self, task_id, signature, retry=False):
        expected_count = 1 if retry else 0
        required_status = "NOT_SENT" if retry else "UNCERTAIN"
        updated = self.db.conn.execute(
            "UPDATE tasks SET send_attempt_count = send_attempt_count + 1, "
            "send_status = 'UNCERTAIN' "
            "WHERE account_id = %s AND id = %s AND message_signature = %s "
            "AND send_attempt_count = %s AND send_status = %s",
            (self.account_id, task_id, signature, expected_count, required_status),
        )
        self.db.conn.commit()
        return updated.rowcount == 1

    async def update_send_verification(self, task_id, signature, status, source):
        updated = self.db.conn.execute(
            "UPDATE tasks SET send_status = %s, verification_source = %s, "
            "last_verified_at = CURRENT_TIMESTAMP "
            "WHERE account_id = %s AND id = %s AND message_signature = %s",
            (status, source, self.account_id, task_id, signature),
        )
        # Only UI-verified delivery closes the dedup row. NOT_SENT/UNCERTAIN
        # leave it IN_FLIGHT so the owning task can still finish or retry it.
        if status == MessageSendState.SENT.value:
            self.db.conn.execute(
                "UPDATE send_tasks SET state = %s, updated_at = %s "
                "WHERE account_id = %s AND signature = %s",
                (
                    SendTaskState.SENT.value, _now().isoformat(),
                    self.account_id, signature,
                ),
            )
        self.db.conn.commit()
        return updated.rowcount == 1

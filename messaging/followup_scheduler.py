"""Periodically nudges conversations the customer has gone silent on.

A "silent" conversation is one whose latest message is ours (OUTBOUND) and
that has at least one real customer message somewhere in its history - this
only re-engages someone already talking to the shop, it never cold-messages a
stranger. Each rule fires at most once per (conversation, rule, the
customer's most recent message): the customer replying again is what earns a
future silence another nudge - the exact same silence is never nagged twice,
because the dedup key below is keyed on that customer message, not on time.

Runs entirely off the database, never the open browser tab, so it costs
nothing when nothing matches - only a matching conversation ever reaches the
single Chrome worker, and only as an ordinary `send_message` task queued
behind whatever real customer-facing work is already waiting.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from agent.reply_language import fixed_text_in_language
from models.tasks import Task


logger = logging.getLogger(__name__)

# Below every routine task (send_message/scan_inbox default to 5, inbox scans
# to 3, a human's own manual send to 100) - a nudge the operator configured in
# advance is the least time-critical thing this worker ever does, and must
# never delay it answering someone who just wrote in.
FOLLOW_UP_PRIORITY = 2


class FollowUpScheduler:
    def __init__(
        self, database, task_repository, policy, *, platform="tiktok_web",
        interval_seconds=60, schedule=None, state=None, reply_language=None,
    ):
        self.database = database
        self.task_repository = task_repository
        self.policy = policy
        self.platform = platform
        self.interval_seconds = max(10.0, float(interval_seconds))
        # Operating hours / pause: identical gate to InboxScanScheduler - a
        # nudge is exactly the kind of new work that must not start outside
        # them, while a send already queued is left to finish normally.
        self.schedule = schedule
        self.state = state
        self._closed_logged = False
        self._paused_logged = False
        # Callable returning the shop's current `messaging.reply_language`.
        # A rule written in another language is skipped, never sent: the shop
        # speaks only its configured language. None = no check (tests).
        self.reply_language = reply_language

    def _accepting_work(self) -> bool:
        if self.state is not None and self.state.paused:
            if not self._paused_logged:
                logger.info(
                    "[FOLLOWUP] agent paused (%s); not scanning for follow-ups",
                    self.state.pause_reason or "no reason given",
                )
                self._paused_logged = True
            return False
        self._paused_logged = False
        if self.schedule is None or self.schedule.is_open():
            self._closed_logged = False
            return True
        if not self._closed_logged:
            logger.info(
                "[FOLLOWUP] outside operating hours (%s); not scanning for follow-ups",
                self.schedule.describe(),
            )
            self._closed_logged = True
        return False

    async def run(self):
        while True:
            try:
                if self._accepting_work():
                    await self._scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("followup_scan_failed", exc_info=True)
            await asyncio.sleep(self.interval_seconds)

    async def _scan_once(self):
        rules = self.policy.enabled_rules()
        if not rules:
            return
        for rule in rules:
            try:
                await self._apply_rule(rule)
            except Exception:
                logger.warning(
                    "followup_rule_failed rule_id=%s", rule.get("id"), exc_info=True,
                )

    async def _apply_rule(self, rule):
        if self.reply_language is not None:
            language = self.reply_language()
            if not fixed_text_in_language(language, rule.get("message")):
                logger.warning(
                    "[FOLLOWUP] rule %s skipped: its message is not in the shop's "
                    "reply language (%s)", rule.get("id"), language,
                )
                return
        account_id = self.task_repository.account_id
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=rule["delay_minutes"])
        # One row per conversation: its latest message, and (via the LATERAL
        # join) the id of the customer's own latest message - the inner join
        # is what drops a conversation with no customer message at all.
        rows = self.database.conn.execute(
            """
            SELECT
                last.conversation_id AS conversation_id,
                inbound.message_id AS anchor_message_id,
                profile.username AS username,
                profile.display_name AS display_name
            FROM (
                SELECT DISTINCT ON (conversation_id)
                    conversation_id, direction, created_at
                FROM conversation_messages
                WHERE account_id = %(account)s
                ORDER BY conversation_id, created_at DESC, id DESC
            ) last
            JOIN LATERAL (
                SELECT message_id
                FROM conversation_messages AS inbound_message
                WHERE inbound_message.account_id = %(account)s
                  AND inbound_message.conversation_id = last.conversation_id
                  AND inbound_message.direction = 'INBOUND'
                ORDER BY inbound_message.created_at DESC, inbound_message.id DESC
                LIMIT 1
            ) inbound ON TRUE
            LEFT JOIN customer_profile AS profile
                ON profile.account_id = %(account)s
               AND profile.conversation_id = last.conversation_id
            WHERE last.direction = 'OUTBOUND'
              AND last.created_at <= %(cutoff)s
            """,
            {"account": account_id, "cutoff": cutoff},
        ).fetchall()
        for row in rows:
            await self._maybe_enqueue(rule, row)

    async def _maybe_enqueue(self, rule, row):
        conversation_id = row["conversation_id"]
        anchor = row["anchor_message_id"]
        recipient = row["username"] or row["display_name"] or conversation_id
        account_id = self.task_repository.account_id
        # Deterministic per (rule, conversation, customer's last message) -
        # stable across scan ticks, so a repeat scan before this is claimed
        # is a cheap no-op, and it never reappears once handled (success or
        # failure) unless the customer writes in again and moves the anchor.
        task_id = f"followup-{rule['id']}-{conversation_id}-{anchor}"
        existing = self.database.conn.execute(
            "SELECT id FROM tasks WHERE account_id = %s AND id = %s",
            (account_id, task_id),
        ).fetchone()
        if existing is not None:
            return
        await self.task_repository.submit(Task(
            id=task_id,
            account_id=account_id,
            capability="send_message",
            platform=self.platform,
            priority=FOLLOW_UP_PRIORITY,
            payload={
                "conversation_id": conversation_id,
                "recipient": recipient,
                "response": rule["message"],
                "confirm_send": True,
                # Never "human": PolicyEngine.allow's conversation_control
                # check (core/policy.py) already refuses any agent-origin
                # send unless the conversation is in AUTO mode, so a
                # conversation a human has taken over (or that is merely
                # WAITING_HUMAN) is skipped automatically, with no separate
                # check needed here.
                "origin": "agent",
                "client_message_id": task_id,
                "inbound_watermark": anchor,
                "automatic": True,
                "followup_rule_id": rule["id"],
            },
        ))
        logger.info(
            "[FOLLOWUP] queued rule_id=%s conversation=%s anchor=%s delay_minutes=%s",
            rule["id"], conversation_id, anchor, rule["delay_minutes"],
        )

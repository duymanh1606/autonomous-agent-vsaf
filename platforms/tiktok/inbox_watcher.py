import asyncio
import logging
import re
from collections import Counter
from hashlib import sha1
from time import monotonic
from uuid import uuid4

from agent.reply_language import fixed_text_in_language, request_ack
from agent.reply_service import ConversationContext
from agent.reply_timing import ReplyTimer
from core.logging_safety import content_fingerprint
from models.action import Action
from models.conversation_view import ConversationView, direction_debug_block
from models.message_delivery import MessageSendState
from models.result import ActionResult
from models.tasks import DEFAULT_TASK_PRIORITY, Task
from platforms.tiktok.scan_outcomes import (
    COMPLETED, FAILED, PENDING, POLICY_REJECTED, REQUIRES_HUMAN, SEND_UNVERIFIED, UNANSWERED,
    outcome_for,
)
from platforms.tiktok.screens import TikTokScreen
from skills.base import Skill


logger = logging.getLogger(__name__)


# TikTok renders the inbox as a drawer over the feed, so an open conversation
# still has the conversation list next to it. Both screens are "inside
# Messages" and neither of them needs `platform.open_inbox` again.
MESSAGING_SCREENS = frozenset({TikTokScreen.MESSAGES, TikTokScreen.CONVERSATION})

# How many extra times the pre-send recheck re-reads the live thread when
# the first read finds nothing, and how long it waits between tries. This is
# a short, bounded grace period for a screen that is still settling right
# after an action (e.g. just after Accept) - not a substitute for the
# fallback_view path below it, which covers the case where no amount of
# waiting helps because the live thread never renders the pre-accept
# message at all.
PRE_SEND_RECHECK_RETRIES = 2
PRE_SEND_RECHECK_RETRY_DELAY_SECONDS = 1.5

# How often a hold asks whether it should stop. Deliberately independent of
# `reply_wait_poll_seconds`, which paces the far more expensive DOM re-read:
# a takeover or a queued staff reply is two indexed queries, and the agent
# owning one Chrome session is exactly why it should notice quickly.
HOLD_INTERRUPT_CHECK_SECONDS = 1.0

# What every possible outcome of one conversation means - settled or owed,
# whether it spends a retry, how many it gets, which metric it lands in - is
# one table in platforms/tiktok/scan_outcomes.py. It used to be five separate
# collections in this file, which is how a status could be added to four of
# them and forgotten in the fifth.


class InboxScanSkill(Skill):
    """Deterministic, state-driven DOM-first inbox flow.

    The skill never fires a fixed click sequence; it decides from the screen it
    is currently on:

    * ``HOME``/anything else - read the Messages badge, then click the Messages
      nav entry and wait for the Messages surface;
    * ``MESSAGES`` - scan the conversation rows in the panel/page;
    * ``CONVERSATION`` - read the *open thread* first, then scan the
      conversation list inside the same panel:
      ``check active inbound -> reply if needed -> scan other unread rows``.
      `platform.open_inbox` is never called again from this state.

    The open thread is checked before the rows because TikTok clears the unread
    marker of the conversation it is currently showing. `unread_rows=0` is
    therefore never evidence that the open conversation has nothing new; only
    its rendered messages are.

    For every unread row it then runs the full handling flow:
    open -> read latest message -> analyze -> generate reply -> send -> verify.
    Reply generation lives behind `reply_provider`, so the flow runs without any
    model credentials; without a provider the inbound message is left to the
    asynchronous message pipeline, which is still a successful scan.

    Only the Messages badge can start this flow; the Activity/notification
    badge never does.
    """

    name = "messaging_inbox_scan"
    capabilities = ["messaging.scan_inbox", "check_unread_messages"]

    def __init__(
        self, max_conversations_per_scan=5, reply_provider=None, reply_skill=None,
        reply_quiet_period_seconds=0, reply_wait_timeout_seconds=45,
        reply_wait_poll_seconds=2, handoff=None, ledger=None,
        conversation_control=None,
        auto_accept_message_requests=False,
        request_slot_reservation=1,
        trust_negative_unread_badge=False,
        message_request_retry_prompt=(
            "Dạ shop đã nhận được tin nhắn, mình kiểm tra và phản hồi ngay ạ."
        ),
        sync_read_elsewhere=True,
        sync_rows=10,
        sync_per_scan=1,
        sync_cooldown_seconds=300,
        timings=None,
    ):
        self.max_conversations_per_scan = max(1, min(20, int(max_conversations_per_scan)))
        self.reply_provider = reply_provider
        self.reply_skill = reply_skill
        # Where "a human must take over" goes. Without it the skill can only
        # log, which is how conversations the agent refused to answer stopped
        # reaching anyone: no escalation row, no dashboard notification.
        self.handoff = handoff
        self.conversation_control = conversation_control
        # Attached post-hoc by main.py once the control server exists, exactly
        # like `self.handoff.events` - realtime dashboard events are optional.
        self.events = None
        # How long the unanswered messages must stay unchanged before the agent
        # answers them. `0` replies on the scan that finds them.
        self.reply_quiet_period_seconds = max(0.0, float(reply_quiet_period_seconds))
        # One conversation is finished before the next is opened, so the quiet
        # period is waited out *in place* instead of deferring the customer to a
        # later scan. The cap keeps one silent thread from eating the whole task
        # budget; hitting it leaves the customer for the next scan.
        self.reply_wait_timeout_seconds = max(0.0, float(reply_wait_timeout_seconds))
        self.reply_wait_poll_seconds = max(0.5, float(reply_wait_poll_seconds))
        self.auto_accept_message_requests = bool(auto_accept_message_requests)
        self.request_slot_reservation = max(0, int(request_slot_reservation))
        # TikTok's Messages badge is an optimization hint, not durable proof
        # that no customer is waiting. Some builds update the row/preview before
        # the navigation badge, and opening a thread clears the badge even when
        # processing fails. Correctness therefore defaults to scanning the
        # panel on every scheduled pass. Operators may explicitly trust the
        # badge only when their build has been verified to keep it reliable.
        self.trust_negative_unread_badge = bool(trust_negative_unread_badge)
        self.message_request_retry_prompt = str(
            message_request_retry_prompt or ""
        ).strip()
        if 0 < self.reply_wait_timeout_seconds <= self.reply_quiet_period_seconds:
            # A cap at or below the quiet period can never be reached, so every
            # customer would silently fall back to "answered by a later scan".
            logger.warning(
                "[INBOX] reply_wait_timeout_seconds=%.1f is not above "
                "reply_quiet_period_seconds=%.1f; no hold can ever settle",
                self.reply_wait_timeout_seconds, self.reply_quiet_period_seconds,
            )
        # conversation_id -> the inbound message this watcher already settled.
        # It is a *hint* that keeps repeated scans of a still-open thread from
        # re-processing the same message; the rendered thread stays the only
        # authority on whether a customer was answered.
        self._settled = {}
        # conversation_id -> the burst being waited on: its signature and when
        # it was first seen unchanged.
        self._pending = {}
        # The last thread printed by `_log_thread_once`, so a hold that re-reads
        # every few seconds does not repeat the same messages in the log.
        self._last_read_signature = None
        # Opening a thread clears its unread marker, so a conversation that was
        # read but not answered disappears from `list_unread_conversations` and
        # would never be scanned again. Remembering it here is what brings the
        # agent back to it. key -> the name needed to reopen it.
        self._unanswered = {}
        # Durable copy of `_settled` and `_unanswered`. Without it a restart
        # loses every customer that was read but not answered - and their
        # unread marker is already gone, so nothing else would ever bring them
        # back. `None` keeps the skill runnable without a database.
        self.ledger = ledger
        self._ledger_loaded = False
        # Where each reply's step timings are stored (storage/reply_timings.py).
        # `None` still logs them; a failed write never fails the scan.
        self.timings = timings
        # The last-seen values of TikTok's own live Activity badge and of
        # real Notification() calls - see `_maybe_reload_for_new_activity`.
        # `None` means "not observed yet in this process", which
        # deliberately never reloads on its own first reading.
        self._last_activity_count = None
        self._last_notifications_seen = None
        # Conversations read on another device - the TikTok phone app, Seller
        # Center - lose their unread marker there, so the scan above never
        # opens them and their messages never reach the database or the
        # dashboard. On a scan with nothing unread, the top rows' previews are
        # compared with what was last synced and one changed row is opened to
        # copy its thread (`_sync_read_elsewhere`). Never answered: a person
        # on that device already saw it.
        self.sync_read_elsewhere = bool(sync_read_elsewhere)
        self.sync_rows = max(1, min(30, int(sync_rows)))
        self.sync_per_scan = max(1, min(5, int(sync_per_scan)))
        self.sync_cooldown_seconds = max(0.0, float(sync_cooldown_seconds))
        # conversation key -> preview signature as of the last sync, and when
        # each was last attempted. In memory: after a restart the top rows are
        # simply synced once more, one per idle scan.
        self._synced_previews = {}
        self._sync_attempted_at = {}

    async def can_handle(self, task, context):
        return task.capability in self.capabilities

    async def execute(self, task, context):
        if task.platform != "tiktok_web":
            return ActionResult(
                success=False,
                error="Automatic inbox scanning is available only for TikTok Web",
            )

        # TikTok's message-request queue does not refresh itself while the
        # page sits loaded (confirmed live - see the method's own docstring).
        # This is the only place that can still catch a stranger's first
        # message: it fires off the Activity badge, which does update live,
        # and only reloads when that badge just went up - never on a timer,
        # so a workspace with nothing new to show never reloads and the
        # idle-based auto-sleep decision is unaffected.
        await self._maybe_reload_for_new_activity(context)

        screen = context.observation.screen
        logger.info("[INBOX] scan started task_id=%s screen=%s", task.id, screen)
        # The ledger from the previous run, read once per process: which
        # customers are still waiting, and which messages were already answered.
        await self._restore_ledger()

        active = None
        if screen == TikTokScreen.CONVERSATION:
            # A conversation is open inside the Messages panel. Its own messages
            # are read first - TikTok marks the open thread as read on arrival,
            # so its row carries no unread flag even when the customer just
            # wrote. The panel list is scanned afterwards, for the other chats.
            logger.info(
                "[INBOX] conversation open; reading the active thread before the panel rows"
            )
            active = await self._handle_active_conversation(task, context)
        elif screen == TikTokScreen.MESSAGES:
            logger.info("[INBOX] messages surface open; scanning conversation rows")
        else:
            skipped = await self._detect_and_open(task, context)
            if skipped is not None:
                return skipped

        request_count = 0
        if self.auto_accept_message_requests:
            requests = await context.execute_action(Action(
                name="platform.detect_message_requests",
                reason="Read the separate TikTok Message requests badge",
            ))
            if requests.success:
                request_count = int(
                    (requests.data.get("tool", {}) or {}).get("count", 0) or 0
                )

        # Reserve one slot for first-contact customers so a constantly busy
        # primary inbox cannot starve Message requests forever.
        primary_limit = self.max_conversations_per_scan
        if request_count and primary_limit > 1:
            primary_limit -= min(
                self.request_slot_reservation, primary_limit - 1
            )
        listed = await context.execute_action(Action(
            name="platform.list_unread_conversations",
            args={"limit": primary_limit},
            reason="Read only conversation rows inside the Messages panel",
        ))
        if not listed.success:
            return listed
        conversations = list(
            listed.data.get("tool", {}).get("conversations", [])
        )
        # The thread that was just handled above still shows up as a row; it is
        # not opened a second time in the same scan.
        done = self._conversation_key((active or {}).get("conversation"))
        # Conversations read but left unanswered by an earlier scan no longer
        # carry an unread marker, so they are re-added by name here.
        fresh_names = {self._conversation_key(item) for item in conversations}
        revisit = []
        seen_revisit_names = set()
        for key, record in self._unanswered.items():
            name = record.get("name") or key
            name_key = self._conversation_key(name)
            # Old databases can contain both the stable TikTok thread id and
            # the username for one conversation. Compare and deduplicate by
            # the reopenable name, otherwise the watcher clicks it twice.
            if (
                key == done or name_key == done or name_key in fresh_names
                or not name_key or name_key in seen_revisit_names
            ):
                continue
            seen_revisit_names.add(name_key)
            revisit.append(name)
        # Durable unanswered rows have already lost TikTok's unread marker and
        # remain first, but they must not consume the entire scan. Reserve one
        # primary slot for a currently unread row whenever both sources have
        # work. Without this, three stale ledger records repeatedly displaced
        # a real new customer even though TikTok reported unread=1.
        eligible_revisit = [
            target for target in revisit
            if not done or self._conversation_key(target) != done
        ]
        eligible_fresh = [
            target for target in conversations
            if not done or self._conversation_key(target) != done
        ]
        if eligible_revisit and eligible_fresh:
            revisit_budget = max(0, primary_limit - 1)
            selected_revisit = eligible_revisit[:revisit_budget]
            fresh_budget = primary_limit - len(selected_revisit)
            remaining = selected_revisit + eligible_fresh[:fresh_budget]
        else:
            remaining = (eligible_revisit + eligible_fresh)[:primary_limit]
        logger.info(
            "[INBOX] unread conversations=%s pending=%s active_handled=%s",
            len(conversations), len(remaining), (active or {}).get("status", "none"),
        )
        handled = [active] if active else []
        if not remaining and not request_count:
            # Nothing unread: the scan's spare time copies threads that were
            # read on another device into the database. Optional work - its
            # failure must never fail the scan that already succeeded.
            try:
                handled.extend(await self._sync_read_elsewhere(task, context, skip={done}))
            except Exception:
                logger.exception("[INBOX] sync of conversations read elsewhere failed")
            # The panel was opened and scanned; an empty result is a real
            # answer rather than a missing signal, and never a reason to click
            # the Messages nav entry again.
            return ActionResult(success=True, data={
                "unread_detected": len(conversations),
                "conversations_observed": 0,
                "active_conversation": active,
                "conversations_handled": handled,
                "replies_sent": self._replies_sent(handled),
                "metrics": self._scan_metrics(
                    handled, request_count=request_count,
                    scanned=len(conversations),
                ),
                "reason": (
                    "conversation_panel_no_unread"
                    if screen == TikTokScreen.CONVERSATION
                    else "panel_scanned_no_unread"
                ),
            })

        revisit_keys = {self._conversation_key(name) for name in revisit}
        opened_count = 0
        for target in remaining:
            if context.observation.screen not in MESSAGING_SCREENS:
                inbox = await context.execute_action(Action(
                    name="platform.open_inbox",
                    reason="Return to Messages before opening the next unread conversation",
                ))
                if not inbox.success:
                    return inbox
            # A revisited thread was already read, so its unread marker is gone
            # and only the plain open action can reach it.
            is_revisit = self._conversation_key(target) in revisit_keys
            timer = ReplyTimer()
            with timer.phase("open"):
                opened = await context.execute_action(Action(
                    name=(
                        "platform.open_conversation" if is_revisit
                        else "platform.open_unread_conversation"
                    ),
                    args={"target": target},
                    reason=(
                        "Reopen a conversation that was read but never answered"
                        if is_revisit
                        else "Open one exact conversation that is still explicitly unread"
                    ),
                ))
            if not opened.success:
                logger.warning(
                    "inbox_scan_skipped target_index=%s reason=unread_state_changed",
                    opened_count,
                )
                # A click can reach the conversation and clear TikTok's unread
                # marker even when post-click verification times out. Recover
                # from the fresh observation when possible; otherwise persist
                # the customer for a plain-name reopen on the next scan. Merely
                # continuing here used to make that customer disappear.
                active_target = self._active_conversation(context.observation)
                if (
                    active_target
                    and self._conversation_key(active_target)
                    == self._conversation_key(target)
                ):
                    started = monotonic()
                    outcome = await self._handle_conversation(
                        task, context, target, timer=timer,
                    )
                    outcome.setdefault("processing_seconds", monotonic() - started)
                else:
                    outcome = {
                        "conversation": target,
                        "conversation_id": self._conversation_id(
                            context.observation, target
                        ),
                        "status": "open_unverified",
                        "error": opened.error or "conversation_open_not_verified",
                    }
                await self._settle(
                    outcome.get("conversation_id") or target, outcome
                )
                handled.append(outcome)
                continue
            opened_count += 1
            started = monotonic()
            outcome = await self._handle_conversation(task, context, target, timer=timer)
            outcome.setdefault("processing_seconds", monotonic() - started)
            await self._settle(outcome.get("conversation_id") or target, outcome)
            handled.append(outcome)

        request_slots = max(0, self.max_conversations_per_scan - opened_count)
        requests_handled = []
        if request_count and request_slots:
            requests_handled = await self._handle_message_requests(
                task, context, min(request_count, request_slots)
            )
            handled.extend(requests_handled)
        return ActionResult(success=True, data={
            "unread_detected": len(conversations),
            "message_requests_detected": request_count,
            "message_requests_handled": len(requests_handled),
            "conversations_observed": opened_count,
            "active_conversation": active,
            "conversations_handled": handled,
            "replies_sent": self._replies_sent(handled),
            "metrics": self._scan_metrics(
                handled, request_count=request_count,
                scanned=len(conversations) + request_count,
            ),
        })

    # Relative times and dates a conversation row shows beside its preview
    # ("2h", "14:05", "3/10", "Hôm qua"). They change while nothing else does,
    # so they are not part of what "the preview changed" means.
    _PREVIEW_TIME = re.compile(
        r"\b\d{1,2}:\d{2}\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"
        r"|\b\d+\s*(?:s|m|h|d|w|y|min|mins|hr|hrs|giây|phút|giờ|ngày|tuần|tháng|năm)\b"
        r"|\b(?:yesterday|today|just now|hôm qua|hôm nay|vừa xong)\b",
        re.IGNORECASE,
    )

    @classmethod
    def _preview_signature(cls, preview):
        return " ".join(cls._PREVIEW_TIME.sub(" ", str(preview or "")).split()).casefold()

    async def _sync_read_elsewhere(self, task, context, *, skip=()):
        """Copy into the database one thread read on another device.

        Only rows that are not unread (the scan handles those), not message
        requests, and not owed an answer by the ledger qualify; among them,
        the first whose preview differs from the last sync - or that was never
        synced in this process - is opened. Its thread is stored like any
        other, its newest customer message settled so the next scan (which
        starts from this open thread) does not answer it, and nothing is
        generated or sent. Any failure just skips the sync until next time.
        """
        persist_thread = getattr(self.reply_provider, "persist_thread", None)
        if not self.sync_read_elsewhere or persist_thread is None:
            return []
        listed = await context.execute_action(Action(
            name="platform.list_conversations",
            args={"limit": self.sync_rows},
            reason="Read the newest conversation rows and their previews",
        ))
        if not listed.success:
            logger.warning(
                "[INBOX] sync of conversations read elsewhere skipped: %s",
                listed.error or "listing conversations failed",
            )
            return []
        rows = (listed.data.get("tool", {}) or {}).get("rows") or []
        owed = {self._conversation_key(record.get("name") or key)
                for key, record in self._unanswered.items()} | set(self._unanswered)
        skip_keys = {self._conversation_key(item) for item in skip if item} - {""}
        now = monotonic()
        candidates = []
        for row in rows[: self.sync_rows]:
            name = str(row.get("name") or "").strip()
            key = self._conversation_key(name)
            if (
                not key or row.get("unread") or row.get("message_request")
                or key in skip_keys or key in owed
            ):
                continue
            signature = self._preview_signature(row.get("preview"))
            if self._synced_previews.get(key) == signature:
                continue
            last_attempt = self._sync_attempted_at.get(key)
            if last_attempt is not None and now - last_attempt < self.sync_cooldown_seconds:
                continue
            candidates.append((name, key, signature))

        synced = []
        for name, key, signature in candidates[: self.sync_per_scan]:
            self._sync_attempted_at[key] = now
            if context.observation.screen not in MESSAGING_SCREENS:
                inbox = await context.execute_action(Action(
                    name="platform.open_inbox",
                    reason="Return to Messages before syncing a conversation",
                ))
                if not inbox.success:
                    break
            opened = await context.execute_action(Action(
                name="platform.open_conversation",
                args={"target": name},
                reason="Open a conversation read on another device to copy its messages",
            ))
            if not opened.success:
                logger.info("[INBOX] sync could not open conversation=%s; later", name)
                continue
            view = self._conversation_view(context.observation)
            if not view.messages:
                view = await self._reread_view(context) or view
            if not view.messages:
                logger.info("[INBOX] sync read no message conversation=%s; later", name)
                continue
            conversation_id = self._conversation_id(context.observation, name)
            pending = view.pending_inbound()
            message = pending[-1] if pending else view.latest_inbound
            last = view.messages[-1]
            await persist_thread(ConversationContext(
                platform=task.platform,
                conversation_key=conversation_id,
                message_id=last.id,
                message_text=last.text,
                customer_name=name,
                avatar_url=self._active_avatar_url(context.observation, name),
                history=view.as_history(),
            ))
            outcome = {
                "conversation": name,
                "conversation_id": conversation_id,
                "status": "synced_read_elsewhere",
                "messages": len(view.messages),
            }
            if message is not None and not self._is_settled(conversation_id, message):
                outcome["inbound_message_id"] = message.id
                outcome["inbound_fingerprint"] = self._fingerprint(message)
                await self._settle(conversation_id, outcome, message)
            self._synced_previews[key] = signature
            logger.info(
                "[INBOX] synced conversation=%s messages=%s (read on another device)",
                name, len(view.messages),
            )
            synced.append(outcome)
        return synced

    async def _handle_message_requests(self, task, context, limit):
        """Accept and process exact first-contact rows from the request queue."""
        opened_list = await context.execute_action(Action(
            name="platform.open_message_requests",
            reason="Open the separate TikTok Message requests list",
        ))
        if not opened_list.success:
            logger.warning("[INBOX] could not open Message requests")
            return []
        listed = await context.execute_action(Action(
            name="platform.list_message_requests",
            args={"limit": limit},
            reason="List exact customers waiting for first-contact approval",
        ))
        if not listed.success:
            return []
        targets = list((listed.data.get("tool", {}) or {}).get("conversations", []))
        handled = []
        try:
            for target in targets[:limit]:
                request_started = monotonic()
                opened = await context.execute_action(Action(
                    name="platform.open_message_request",
                    args={"target": target},
                    reason="Open one exact pending customer request",
                ))
                if not opened.success:
                    handled.append({
                        "conversation": target,
                        "status": "request_open_failed",
                    })
                    continue
                # TikTok may remove the original request bubble immediately
                # after Accept. Preserve the last readable inbound view before
                # that transition so it can still be analysed when available.
                request_view = self._conversation_view(context.observation)
                if request_view.latest_inbound is None:
                    # The observation right after the click is routinely a
                    # state the extension published before the preview had
                    # rendered its bubbles - the request preview has no
                    # composer, so the extension reports MESSAGES rather than
                    # CONVERSATION and the observation carries no thread at
                    # all. Giving up on that single read is what withheld a
                    # real customer as `pre_accept_content_unreadable` and
                    # escalated them to a human instead of accepting them
                    # (confirmed live). Every other read path in this file
                    # re-reads before concluding a thread is unreadable; this
                    # one now does too, with the same bounded grace period.
                    fresh = await self._reread_request_view(context, target)
                    if fresh is not None:
                        request_view = fresh
                request_message = request_view.latest_inbound
                if request_message is not None and self._is_settled(target, request_message):
                    # TikTok can keep listing an already-accepted request for a
                    # while after Accept, so without this check every scan
                    # (every ~20s) reopened it, clicked Accept again (a
                    # risk=high action), and re-read the whole thread just to
                    # rediscover "already handled" at the end. The preview
                    # already carries the same inbound message id/fingerprint
                    # the earlier scan settled, so that expensive round trip
                    # is skippable before Accept is ever clicked.
                    logger.info(
                        "[INBOX] message request already settled; skipping "
                        "re-accept conversation=%s", target,
                    )
                    handled.append({
                        "conversation": target,
                        "conversation_id": self._conversation_id(context.observation, target),
                        "status": "already_handled",
                        "message_request": True,
                        "inbound_message_id": request_message.id,
                        "processing_seconds": monotonic() - request_started,
                    })
                    # Same reasoning as the branches below: an unaccepted
                    # preview gives no safe way to navigate to the next row.
                    break
                classification, classification_reason = self._classify_request(
                    target, request_view
                )
                request_key = self._conversation_key(target)
                snapshot_saved = await self._persist_required(
                    "save_pre_accept_snapshot", request_key,
                    username=target,
                    conversation_id=self._conversation_id(context.observation, target),
                    messages=request_view.as_history(),
                    classification=classification,
                    status=("PENDING" if classification == "valid" else "WAITING_HUMAN"),
                )
                if not snapshot_saved:
                    classification = "unknown"
                    classification_reason = "pre_accept_snapshot_not_persisted"
                if classification != "valid":
                    logger.warning(
                        "[INBOX] Message request withheld before Accept conversation=%s "
                        "classification=%s reason=%s",
                        target, classification, classification_reason,
                    )
                    await self._hand_over(
                        self._conversation_id(context.observation, target), target,
                        f"message_request_{classification}:{classification_reason}",
                        request_message,
                    )
                    handled.append({
                        "conversation": target,
                        "conversation_id": self._conversation_id(
                            context.observation, target
                        ),
                        "status": "requires_human",
                        "reason": classification_reason,
                        "message_request": True,
                        "processing_seconds": monotonic() - request_started,
                    })
                    # Do not guess how to navigate from an unaccepted preview to
                    # another request row. The reserved request slot on the next
                    # scan will continue safely.
                    break
                accepted = await context.execute_action(Action(
                    name="platform.accept_message_request",
                    args={"target": target},
                    reason="Accept the exact request so the normal reply composer is available",
                ))
                if not accepted.success:
                    handled.append({
                        "conversation": target,
                        "status": "request_accept_failed",
                    })
                    # The current preview may cover the request list. Returning
                    # to the primary list is safer than guessing the next row.
                    break
                await self._persist("mark_request_accepted", request_key)
                conversation_id = (
                    self._conversation_id(context.observation, target) or target
                )
                # Accept only unlocks the ability to reply at all. A short,
                # fixed confirmation goes out immediately so the stranger is
                # not left looking at silence - it is never an improvised
                # guess, so it is never wrong. It is a courtesy only: this
                # one-shot send is never retried and never settles the
                # conversation, so it can never block the real answer below.
                await self._send_accept_ack_reply(task, context, target, conversation_id)
                # The stranger's own pre-accept message is then answered for
                # real, through the exact same LLM pipeline every other
                # conversation uses - `fallback_view` supplies its content
                # since TikTok can drop the request bubble right after
                # Accept. `skip_already_replied_check` is required here: the
                # ack just sent is itself an outbound message after this
                # inbound, which would otherwise make the generic "already
                # replied?" guard skip the real answer.
                outcome = await self._handle_conversation(
                    task, context, target, fallback_view=request_view,
                    skip_already_replied_check=True,
                )
                outcome.setdefault(
                    "processing_seconds", monotonic() - request_started
                )
                await self._settle(outcome.get("conversation_id") or target, outcome)
                outcome["message_request"] = True
                handled.append(outcome)
        finally:
            returned = await context.execute_action(Action(
                name="platform.return_to_primary_inbox",
                reason="Return to the primary inbox after processing requests",
            ))
            if not returned.success:
                logger.warning("[INBOX] Message requests processed but primary list was not restored")
        return handled

    @staticmethod
    def _classify_request(target, view):
        """Fail-closed, deterministic gate before the destructive Accept transition.

        This does not try to understand the customer's business intent; Gemini
        runs only after acceptance. It rejects identity/content ambiguity and
        well-defined credential, phishing and bulk-spam signals.
        """
        if not str(target or "").strip():
            return "unknown", "sender_not_identified"
        message = view.latest_inbound
        if message is None:
            return "unknown", "pre_accept_content_unreadable"
        text = " ".join(item.text for item in view.pending_inbound()).strip().casefold()
        if not text and not message.media:
            return "unknown", "pre_accept_content_empty"
        credential_markers = (
            "mật khẩu", "password", "passcode", "mã otp", " otp ",
            "cookie", "session token", "api key", "recovery code",
        )
        if any(marker in f" {text} " for marker in credential_markers):
            return "suspicious", "credential_or_secret_request"
        has_link = bool(re.search(r"(?:https?://|www\.|t\.me/|bit\.ly/)", text))
        scam_markers = (
            "đầu tư", "lợi nhuận", "crypto", "tiền ảo", "nhận thưởng",
            "trúng thưởng", "kiếm tiền", "vay tiền",
        )
        if has_link and any(marker in text for marker in scam_markers):
            return "spam", "unsolicited_financial_link"
        if len(text) > 3000 or re.search(r"(.)\1{24,}", text):
            return "spam", "bulk_or_repeated_content"
        return "valid", "customer_request"

    def _request_ack_text(self) -> str:
        """The configured courtesy line, in the shop's reply language.

        The YAML line is written once, in one language; a shop set to another
        `messaging.reply_language` sends the built-in line of its own language
        instead. Read through the reply provider on every call so a language
        switch from the dashboard applies at once.
        """
        configured = self.message_request_retry_prompt
        language = getattr(self.reply_provider, "reply_language", None)
        if not configured or language is None:
            return configured
        return fixed_text_in_language(language, configured) or request_ack(language)

    async def _send_accept_ack_reply(self, task, context, target, conversation_id):
        """Send a short, fixed confirmation right after accepting a request.

        Deliberately not routed through `reply_provider`/the LLM pipeline: a
        first-contact stranger has not been vetted by a real conversation
        yet, so this courtesy line (`message_request_retry_prompt`) is always
        the same operator-configured text, never an improvised guess. The
        stranger's actual pre-accept message is answered for real right
        after this, through the full LLM pipeline - see the caller.

        This call is one-shot and best-effort: its outcome is never settled
        and never retried, since the real answer that follows is what the
        next scan's dedup/retry logic tracks. A synthetic inbound id is used
        because this is not a reply to any specific inbound message - it is
        the same courtesy line regardless of what was said.
        """
        text = self._request_ack_text()
        if self.reply_skill is None or not text:
            return {
                "conversation": target,
                "conversation_id": conversation_id,
                "status": "ack_not_sent",
            }
        inbound_message_id = (
            "accepted-request-"
            + sha1(str(conversation_id or target).encode("utf-8")).hexdigest()[:16]
        )
        reply_task = Task(
            id=f"{task.id}-request-ack-{uuid4().hex[:8]}",
            account_id=task.account_id,
            capability="reply_to_message",
            platform=task.platform,
            payload={
                "conversation_id": conversation_id,
                "recipient": target,
                "response": text,
                "confirm_send": True,
                "inbound_message_id": inbound_message_id,
            },
        )
        logger.info(
            "[INBOX] message request accepted; sending fixed acknowledgement "
            "instead of a generated reply conversation=%s",
            target,
        )
        result = await self._execute_reply(reply_task, context)
        if result.success and (result.data or {}).get("status") == "already_replied":
            status = "already_replied"
        elif result.success:
            status = "sent"
        else:
            status = "send_unverified"
        return {
            "conversation": target,
            "conversation_id": conversation_id,
            "status": status,
            "reply_task_id": reply_task.id,
            "inbound_message_id": inbound_message_id,
            **({"error": result.error} if not result.success else {}),
        }

    async def _detect_and_open(self, task, context):
        """Badge check then open Messages. Returns a result only when finished."""
        detected = await context.execute_action(Action(
            name="platform.detect_unread",
            reason="Read the Messages badge before leaving the current screen",
        ))
        tool = (detected.data or {}).get("tool") or {}
        logger.info(
            "[INBOX] messages badge present=%s count=%s activity=%s panel_visible=%s",
            tool.get("unread"), tool.get("unread_count", 0),
            tool.get("activity_unread_count", 0),
            tool.get("messages_panel_visible", False),
        )
        if detected.success and self._can_skip_scan(detected):
            logger.info(
                "inbox_scan_skipped task_id=%s reason=no_unread_signal", task.id
            )
            return ActionResult(success=True, data={
                "unread_detected": 0,
                "conversations_observed": 0,
                "reason": "no_unread_signal",
            })
        opened = await context.execute_action(Action(
            name="platform.open_inbox",
            reason="Unread scanning starts from the verified Messages surface",
        ))
        if not opened.success:
            return opened
        return None

    # ------------------------------------------ the currently open thread
    async def _handle_active_conversation(self, task, context):
        """Handle the thread that is already open, before any row is read.

        Returns the outcome dict, or `None` when this state carries no usable
        evidence about an open thread - the panel scan then runs on its own,
        exactly as before.
        """
        target = self._active_conversation(context.observation)
        if not target:
            logger.info(
                "[INBOX] active conversation is not identifiable; "
                "only the panel rows are scanned"
            )
            return None
        if self._active_conversation_is_request(context.observation, target):
            logger.info(
                "[INBOX] active conversation=%s is an unaccepted message "
                "request; left to the dedicated request queue instead of "
                "being read here",
                target,
            )
            return None
        conversation_id = self._conversation_id(context.observation, target)
        inbound = self._conversation_view(context.observation).latest_inbound
        if inbound is not None and self._is_settled(conversation_id, inbound):
            logger.info(
                "[INBOX] active conversation=%s id=%s inbound=%s already settled; "
                "not handled again",
                target, conversation_id, inbound.id,
            )
            return {
                "conversation": target,
                "conversation_id": conversation_id,
                "status": "already_handled",
                "inbound_message_id": inbound.id,
                "source": "active_conversation",
            }
        # The open thread is handled by the same flow an unread row goes
        # through, minus the opening click: it is already on screen.
        outcome = await self._handle_conversation(task, context, target)
        await self._settle(
            outcome.get("conversation_id") or conversation_id, outcome, inbound
        )
        return dict(outcome, source="active_conversation")

    @staticmethod
    def _active_conversation(observation):
        """The username of the open thread, from DOM evidence only.

        The extension's `active_chat` is the trusted header reading; the URL is
        never consulted, since TikTok opens a thread without changing it.
        """
        extension_state = (observation.metadata or {}).get("extension_state") or {}
        active_chat = extension_state.get("active_chat") or {}
        username = str(active_chat.get("username") or "").strip()
        return username if active_chat.get("present") and username else ""

    @classmethod
    def _active_avatar_url(cls, observation, expected_target):
        """The open thread's avatar image URL, from the same DOM header
        reading as `_active_conversation` - only trusted when the open
        thread's own username matches `expected_target`, so a header still
        showing the PREVIOUS conversation never gets attributed to this one."""
        extension_state = (observation.metadata or {}).get("extension_state") or {}
        active_chat = extension_state.get("active_chat") or {}
        if not active_chat.get("present"):
            return ""
        username = str(active_chat.get("username") or "").strip()
        if cls._conversation_key(username) != cls._conversation_key(expected_target):
            return ""
        return str(active_chat.get("avatar_url") or "").strip()

    @classmethod
    def _active_conversation_is_request(cls, observation, target):
        """True when the open thread is an unaccepted message request.

        TikTok renders a pending request's thread as system banners only
        until Accept is clicked, so `_handle_active_conversation` would read
        it as `no_inbound_message` forever. That thread is never answerable
        from here; `_handle_message_requests` (via the dedicated
        `platform.list_message_requests` queue) is the only path that can
        unlock it.
        """
        extension_state = (observation.metadata or {}).get("extension_state") or {}
        wanted = cls._conversation_key(target)
        if not wanted:
            return False
        for row in extension_state.get("conversations") or ():
            name = str(row.get("username") or row.get("text") or "").strip()
            if cls._conversation_key(name) == wanted:
                return bool(row.get("message_request"))
        return False

    @classmethod
    def _conversation_id(cls, observation, target):
        """The platform's stable thread key for `target`, else `target` itself.

        Everything persisted about a customer - profile, facts, history, the
        send-task signature - is keyed on this, so it must survive the customer
        renaming themselves. The panel rows carry the key in both MESSAGES and
        CONVERSATION state, so the open thread can be identified by matching
        its header username against them.
        """
        extension_state = (observation.metadata or {}).get("extension_state") or {}
        wanted = cls._conversation_key(target)
        if not wanted:
            return str(target or "")
        for row in extension_state.get("conversations") or ():
            key = str(row.get("key") or "").strip()
            name = str(row.get("username") or row.get("text") or "").strip()
            if key and cls._conversation_key(name) == wanted:
                return key
        return str(target or "")

    # ---------------------------------------------------- waiting on a burst
    def _burst_signature(self, pending):
        """Identity of one unanswered burst: how many, and which is last."""
        last = pending[-1]
        return sha1(
            f"{len(pending)}|{last.id}|{self._fingerprint(last)}".encode("utf-8")
        ).hexdigest()[:16]

    async def _reread_view(self, context):
        """One fresh DOM read of the open thread as a view, else `None`.

        `None` means the read produced no usable thread at all, which is not
        the same as "the customer has nothing pending": `browser.snapshot`
        returns a Playwright DOM that carries no rendered messages, so reading
        through it would silently empty the burst. The extension-backed
        `read_conversation` is the only source trusted here.
        """
        verifier = getattr(getattr(context, "platform", None), "state_verifier", None)
        reader = getattr(verifier, "read_conversation", None)
        if reader is None:
            return None
        try:
            state = await reader()
        except Exception as exc:
            logger.warning(
                "[INBOX] hold read failed error_type=%s", type(exc).__name__,
                exc_info=True,
            )
            return None
        if state is None:
            return None
        if not getattr(state, "thread_trusted", False):
            logger.error(
                "[INBOX] rejected untrusted thread source=%s",
                getattr(state, "thread_source", "unknown") or "unknown",
            )
            return None
        view = ConversationView.from_dom_state(state)
        self._log_thread_once(view)
        return view if view.messages else None

    async def _reread_request_view(self, context, target):
        """Re-read a just-opened Message request preview, or `None`.

        Same bounded grace period as the pre-send recheck, and for the same
        reason: a screen that is still settling right after a click. Accept is
        a one-way, risk=high transition, so this only ever *adds* evidence -
        a preview that stays unreadable through every attempt still falls
        through to `_classify_request`'s fail-closed `unknown` verdict.
        """
        for attempt in range(PRE_SEND_RECHECK_RETRIES + 1):
            view = await self._reread_view(context)
            if view is not None and view.latest_inbound is not None:
                logger.info(
                    "[INBOX] Message request re-read conversation=%s messages=%s "
                    "attempt=%s (the observation carried none)",
                    target, len(view.messages), attempt + 1,
                )
                return view
            if attempt < PRE_SEND_RECHECK_RETRIES:
                await asyncio.sleep(PRE_SEND_RECHECK_RETRY_DELAY_SECONDS)
        return None

    def _log_thread_once(self, view):
        """Print a re-read thread only when it differs from the last one.

        The hold re-reads every few seconds; logging each message on every poll
        buried the rest of the scan under repeats of the same thread.
        """
        signature = tuple((item.id, item.direction) for item in view.messages)
        if signature == self._last_read_signature:
            return
        self._last_read_signature = signature
        for item in view.messages:
            logger.info(
                "[INBOX] message parsed text_hash=%s characters=%s direction=%s id=%s",
                content_fingerprint(item.text), len(item.text), item.direction, item.id,
            )

    async def _wait_for_customer(self, context, target, conversation_id, pending):
        """Stay on this thread until the burst settles, then let the caller reply.

        Returns `(finished, waited, pending, latest_view, interrupted)`. The
        DOM is re-read on every poll, so a message arriving mid-wait both
        restarts the quiet period and joins the burst being answered.
        Returning `finished=False` means the cap was reached and this customer
        is left to the next scan rather than answered halfway through a
        sentence.

        `interrupted` is why the hold gave up early, and empty when it did
        not. The hold occupies the single Chrome session for up to
        `reply_wait_timeout_seconds`, so two things have to be able to cut it
        short instead of waiting for the cap:

        * `human_controlled` - somebody clicked Takeover. The check used to
          run only *after* the hold, so a takeover during it was noticed up to
          30s late.
        * `yielded` - a staff reply is queued behind this scan. It is
          submitted at priority 100 so it is claimed the moment this task
          ends; measured on this workspace, that still meant a p95 of 11.1s
          spent waiting for a hold to time out.

        Both are fail-closed: giving up the hold means not replying, and the
        customer goes back on the queue for the next scan.

        `latest_view` is the newest successfully-read `ConversationView`, or
        `None` if every re-read failed (or none were needed). The caller must
        use it - not its original, pre-wait view - to build the context that
        gets persisted: that view is what `_build_context` turns into
        `history=view.as_history()`, and a message the customer sends mid-wait
        only exists in a *re-read* view. Persisting the stale one silently
        dropped the second (and later) message of a burst from the dashboard
        even though the reply itself, built from `pending` instead, correctly
        answered all of them.
        """
        started = monotonic()
        deadline = started + self.reply_wait_timeout_seconds
        latest_view = None
        finished, waited = self._customer_finished(conversation_id, pending)
        logger.info(
            "[INBOX] holding conversation=%s messages=%s until the burst settles "
            "(quiet_period=%.1fs cap=%.1fs)",
            target, len(pending), self.reply_quiet_period_seconds,
            self.reply_wait_timeout_seconds,
        )
        blind_reads = 0
        while not finished and monotonic() < deadline:
            interrupted = await self._sleep_between_polls(
                context, conversation_id, target
            )
            if interrupted:
                return (
                    False, monotonic() - started, pending, latest_view, interrupted
                )
            current_view = await self._reread_view(context)
            if current_view is None:
                # The read itself failed. The thread is almost certainly still
                # there, so the burst already in hand is kept and the clock is
                # what decides: a reader that cannot see new messages is no
                # reason to leave a customer who already stopped writing
                # unanswered. The cap still bounds the wait.
                blind_reads += 1
                logger.info(
                    "[INBOX] hold read returned nothing conversation=%s attempt=%s; "
                    "the burst of %s stands",
                    target, blind_reads, len(pending),
                )
                finished, waited = self._customer_finished(conversation_id, pending)
                continue
            latest_view = current_view
            current = current_view.pending_inbound()
            if not current:
                # "Nothing unanswered" is only believable with an answer in the
                # thread to show for it. Without one the read simply saw less
                # than the previous one - a re-render, a scrolled-away bubble -
                # and dropping the burst there is what left the customer
                # unanswered for the whole scan.
                if current_view.already_replied():
                    return False, waited, pending, latest_view, ""
                blind_reads += 1
                logger.info(
                    "[INBOX] hold read showed neither a new message nor an answer "
                    "conversation=%s attempt=%s; keeping the burst of %s",
                    target, blind_reads, len(pending),
                )
                finished, waited = self._customer_finished(conversation_id, pending)
                continue
            blind_reads = 0
            pending = current
            finished, waited = self._customer_finished(conversation_id, pending)
        if not finished:
            # The loop can end on the cap while the last successful read is
            # already older than the quiet period; the clock, not the number of
            # polls, is what says the customer stopped writing.
            finished, waited = self._customer_finished(conversation_id, pending)
        logger.info(
            "[INBOX] hold finished conversation=%s messages=%s waited=%.1fs settled=%s "
            "blind_reads=%s",
            target, len(pending), waited, finished, blind_reads,
        )
        return finished, waited, pending, latest_view, ""

    async def _sleep_between_polls(self, context, conversation_id, target):
        """Wait one DOM poll interval, watching for a reason to stop.

        The DOM is deliberately re-read only every `reply_wait_poll_seconds`
        (a real read costs ~0.1-0.3s and how long a burst has been quiet is
        decided by the clock, not by how often it is checked). Whether a human
        has taken over, or a staff reply is queued, is two indexed queries -
        cheap enough to ask far more often, and the whole point of asking is
        to hand the Chrome session over quickly. So the sleep is sliced and
        the checks run per slice, leaving reply timing exactly as it was.
        """
        remaining = max(0.0, float(self.reply_wait_poll_seconds))
        while True:
            nap = min(HOLD_INTERRUPT_CHECK_SECONDS, remaining)
            await asyncio.sleep(nap)
            remaining -= nap
            interrupted = await self._hold_interrupted(context, conversation_id, target)
            if interrupted:
                return interrupted
            if remaining <= 0:
                return ""

    async def _hold_interrupted(self, context, conversation_id, target):
        """Why this hold must stop now, or `""` to keep holding.

        Both checks are single indexed queries (sub-millisecond); the hold
        already re-reads the DOM on the same cadence, which costs far more.
        """
        if self.conversation_control is not None and not (
            await self.conversation_control.agent_may_reply(conversation_id)
        ):
            logger.info(
                "[INBOX] hold released conversation=%s reason=human_took_over",
                target,
            )
            return "human_controlled"
        repository = getattr(getattr(context, "memory", None), "repository", None)
        pending_above = getattr(repository, "has_pending_above_priority", None)
        if pending_above is None:
            return ""
        try:
            urgent = await pending_above(DEFAULT_TASK_PRIORITY)
        except Exception:
            # Not knowing whether something is queued is no reason to drop a
            # customer mid-burst: keep holding, exactly as before.
            logger.exception("[INBOX] checking the task queue during a hold failed")
            return ""
        if urgent:
            logger.info(
                "[INBOX] hold released conversation=%s reason=priority_task_queued; "
                "the customer is queued for the next scan",
                target,
            )
            return "yielded"
        return ""

    def _customer_finished(self, conversation_id, pending):
        """True once the unanswered messages stopped changing for long enough.

        Every new message restarts the clock, so a customer writing several
        messages in a row gets one answer to all of them instead of being
        interrupted. Timing uses the agent's own clock: TikTok exposes an
        absolute timestamp on a message only rarely.
        """
        key = self._conversation_key(conversation_id)
        signature = self._burst_signature(pending)
        record = self._pending.get(key)
        if record is None or record["signature"] != signature:
            record = {"signature": signature, "first_seen": monotonic()}
            self._pending[key] = record
        waited = monotonic() - record["first_seen"]
        return waited >= self.reply_quiet_period_seconds, waited

    # ------------------------------------------------------- settled ledger
    @staticmethod
    def _conversation_key(target):
        return str(target or "").strip().casefold().lstrip("@")

    @staticmethod
    def _fingerprint(message):
        """Content identity of one inbound message inside its thread.

        The text alone is not enough - a customer may send "alo" twice - so the
        position in the thread is part of it. It complements the message id
        rather than replacing it: ids survive re-renders, fingerprints survive
        a change of id scheme.
        """
        text = " ".join(str(getattr(message, "text", "")).split()).casefold()
        return sha1(
            f"{getattr(message, 'order', 0)}|{text}".encode("utf-8")
        ).hexdigest()[:16]

    def _is_settled(self, target, message):
        record = self._settled.get(self._conversation_key(target))
        if not record or message is None:
            return False
        current_fingerprint = self._fingerprint(message)
        recorded_id = str(record.get("inbound_message_id") or "")
        recorded_fingerprint = str(record.get("fingerprint") or "")
        same_id = recorded_id == message.id
        # Old extension builds generated fallback `dom-*` ids from message
        # text only. A customer repeating the same words therefore collided
        # with the earlier bubble. For fallback ids, a different position/text
        # fingerprint proves this is a distinct inbound message.
        if (
            same_id
            and recorded_id.startswith("dom-")
            and recorded_fingerprint
            and recorded_fingerprint != current_fingerprint
        ):
            same_id = False
        return same_id or recorded_fingerprint == current_fingerprint

    async def _restore_ledger(self):
        """Load the durable ledger once, before the first conversation is read.

        Anything already in memory wins: the process has seen the live thread,
        the stored rows are only what the previous run left behind.
        """
        if self._ledger_loaded or self.ledger is None:
            return
        self._ledger_loaded = True
        try:
            settled, unanswered = await self.ledger.load()
        except Exception:
            # A scan that cannot read its own history is still better than no
            # scan: it simply starts from what the open threads show.
            logger.exception("[INBOX] restoring the ledger failed; starting empty")
            return
        self._settled = {**settled, **self._settled}
        self._unanswered = {**unanswered, **self._unanswered}
        if unanswered:
            logger.info(
                "[INBOX] %s conversation(s) were read but never answered before "
                "the last restart; they are queued again",
                len(unanswered),
            )

    async def _persist(self, call, *args, **kwargs):
        """Write one ledger change through. Never fails the scan."""
        if self.ledger is None:
            return
        try:
            await getattr(self.ledger, call)(*args, **kwargs)
        except Exception:
            logger.exception("[INBOX] persisting the ledger failed call=%s", call)

    async def _persist_required(self, call, *args, **kwargs):
        """Persist a safety prerequisite and report whether it is durable."""
        if self.ledger is None or not hasattr(self.ledger, call):
            logger.error("[INBOX] required persistence is unavailable call=%s", call)
            return False
        try:
            await getattr(self.ledger, call)(*args, **kwargs)
            return True
        except Exception:
            logger.exception("[INBOX] required persistence failed call=%s", call)
            return False

    async def _settle(self, target, outcome, message=None):
        """Remember a finished outcome so the next scan does not repeat it."""
        key = self._conversation_key(target)
        if not key:
            return
        name = (outcome or {}).get("conversation") or target
        name_key = self._conversation_key(name)
        alias_keys = {name_key} - {key, ""}
        # Builds before stable conversation ids were available persisted rows
        # under usernames. Migrate those aliases as soon as the stable id is
        # observed, preserving retry history but leaving only one queue entry.
        prior_records = [
            dict(record) for candidate in ({key} | alias_keys)
            if (record := self._unanswered.get(candidate)) is not None
        ]
        for alias in alias_keys:
            self._unanswered.pop(alias, None)
            self._settled.pop(alias, None)
            self._pending.pop(alias, None)
            await self._persist("forget", alias)
        status = (outcome or {}).get("status", "unknown")
        verdict = outcome_for(status)
        if not verdict.settled:
            # Read but not answered: the unread marker is already gone, so the
            # only way back to this customer is the ledger.
            record = max(
                prior_records,
                key=lambda item: int(item.get("attempts", 0) or 0),
                default={"name": name, "attempts": 0},
            )
            record["name"] = name
            if not verdict.counts_attempt:
                self._unanswered[key] = record
                logger.info(
                    "[INBOX] conversation=%s queued for the next scan "
                    "(status=%s attempt=%s, not counted)",
                    name, status, record["attempts"],
                )
                await self._persist(
                    "mark_unanswered", key,
                    name=name, status=status, attempts=record["attempts"],
                )
                return
            record["attempts"] += 1
            give_up_after = verdict.retry_budget
            if record["attempts"] > give_up_after:
                # Re-opening a thread the agent cannot read makes no progress
                # and would occupy every scan forever. Giving up is exactly the
                # moment a person has to be told: the unread marker is long
                # gone, so nothing else will ever bring this thread back.
                self._unanswered.pop(key, None)
                logger.warning(
                    "[INBOX] conversation=%s gave up after %s attempts "
                    "(status=%s); a human has to look at this thread",
                    name, record["attempts"] - 1, status,
                )
                self._hand_over_later(
                    (outcome or {}).get("conversation_id") or key, name,
                    f"unreadable_after_{record['attempts'] - 1}_attempts:{status}",
                    message,
                )
                await self._persist("forget", key)
                return
            self._unanswered[key] = record
            logger.info(
                "[INBOX] conversation=%s left unanswered (status=%s attempt=%s/%s); "
                "queued for the next scan",
                name, status, record["attempts"], give_up_after,
            )
            await self._persist(
                "mark_unanswered", key,
                name=name, status=status, attempts=record["attempts"],
            )
            return
        inbound_id = outcome.get("inbound_message_id") or (
            message.id if message is not None else ""
        )
        if not inbound_id:
            # Nothing to dedup on (`nothing_to_answer` has no inbound message),
            # but this conversation is finished: a retry row left behind by an
            # earlier failed read must not keep it in the queue.
            if self._unanswered.pop(key, None) is not None:
                self._pending.pop(key, None)
                await self._persist("forget", key)
            return
        record = {
            "inbound_message_id": inbound_id,
            "fingerprint": (
                outcome.get("inbound_fingerprint", "")
                or (self._fingerprint(message) if message is not None else "")
            ),
            "status": outcome["status"],
        }
        self._settled[key] = record
        # This burst is answered; the next one starts its own waiting period.
        self._pending.pop(key, None)
        self._unanswered.pop(key, None)
        await self._persist(
            "settle", key,
            name=(outcome or {}).get("conversation") or target,
            status=record["status"],
            inbound_message_id=record["inbound_message_id"],
            fingerprint=record["fingerprint"],
        )

    def _build_context(
        self, task, context, target, conversation_id, view, message, pending, timer=None,
    ):
        return ConversationContext(
            platform=task.platform,
            conversation_key=conversation_id,
            message_id=message.id,
            # One reply answers the whole burst, so the analyzer sees every
            # unanswered message rather than only the last one.
            message_text="\n".join(item.text for item in pending),
            customer_name=target,
            avatar_url=self._active_avatar_url(context.observation, target),
            history=view.as_history(),
            pending_messages=[
                {"id": item.id, "text": item.text, "index": item.order}
                for item in pending
            ],
            inbound_media=[
                media for item in pending for media in item.media
            ],
            **({"timer": timer} if timer is not None else {}),
        )

    def _announce_message(self, event_type, conversation_id, conversation_name, message):
        """Best-effort dashboard event. Never raises, never blocks the scan."""
        publish = getattr(self.events, "publish", None)
        if publish is None:
            return
        try:
            publish(
                event_type,
                conversation=conversation_name,
                conversation_id=conversation_id,
                message_id=getattr(message, "id", None),
            )
        except Exception:
            logger.exception("[INBOX] publishing %s failed", event_type)

    async def _hand_over(
        self, conversation_id, conversation_name, reason, message=None, analysis=None,
    ):
        """Report one conversation to a human. Never fails the scan.

        `analysis` is what the reply pipeline concluded, kept on the escalation
        row so the reason a reply was withheld can be read back later.
        """
        if self.handoff is None:
            return
        try:
            await self.handoff.open(
                conversation_id=conversation_id,
                conversation_name=conversation_name,
                reason=reason,
                message=message,
                analysis=analysis,
            )
        except Exception:
            logger.exception(
                "[INBOX] handing the conversation to a human failed conversation=%s",
                conversation_name or conversation_id,
            )

    def _hand_over_later(self, conversation_id, conversation_name, reason, message=None):
        """The same, from `_settle`, which its callers invoke synchronously.

        The ledger is bookkeeping, not I/O, so it stays a plain method and the
        handoff is scheduled on the running loop instead of awaited here. The
        warning is already in the log before this runs, so a task that never
        gets scheduled costs the visibility, not the record of what happened.
        """
        if self.handoff is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(
            self._hand_over(conversation_id, conversation_name, reason, message)
        )

    @staticmethod
    def _replies_sent(handled):
        return sum(1 for item in handled if item and item.get("status") == "sent")

    @staticmethod
    def _scan_metrics(handled, *, request_count=0, scanned=0):
        outcomes = [item for item in handled if item]
        statuses = Counter(str(item.get("status") or "unknown") for item in outcomes)
        durations = sorted(
            float(item.get("processing_seconds") or 0) for item in outcomes
            if item.get("processing_seconds") is not None
        )
        average = sum(durations) / len(durations) if durations else 0.0
        p95 = durations[min(len(durations) - 1, int(0.95 * len(durations)))] if durations else 0.0
        # Which number each outcome adds to is part of its row in the
        # vocabulary, so a new status cannot be counted in one place and
        # missed in another.
        buckets = Counter()
        duplicates = 0
        for name, count in statuses.items():
            verdict = outcome_for(name)
            buckets[verdict.bucket] += count
            if verdict.duplicate:
                duplicates += count
        return {
            "scanned": int(scanned),
            "requests_seen": int(request_count),
            "conversations_processed": len(outcomes),
            "replies_sent": statuses["sent"],
            "completed": buckets[COMPLETED],
            "completed_with_pending": buckets[PENDING],
            "unanswered": buckets[UNANSWERED],
            "send_unverified": buckets[SEND_UNVERIFIED],
            "waiting_human": buckets[REQUIRES_HUMAN],
            "failed": buckets[FAILED],
            "skipped_duplicate": duplicates,
            "policy_rejected": buckets[POLICY_REJECTED],
            "retries": sum(int(item.get("retries") or 0) for item in outcomes),
            "average_processing_seconds": round(average, 3),
            "p95_processing_seconds": round(p95, 3),
        }

    # ------------------------------------------------------------- handling
    async def _handle_conversation(self, task, context, target, *args, timer=None, **kwargs):
        """`_read_and_reply`, timed: see agent/reply_timing.py.

        `timer` arrives already holding the open click when the caller opened
        the thread; an already-open thread starts its clock here.
        """
        timer = timer if timer is not None else ReplyTimer()
        outcome = await self._read_and_reply(
            task, context, target, *args, timer=timer, **kwargs
        )
        await self._report_timing(task, outcome, timer)
        return outcome

    async def _report_timing(self, task, outcome, timer):
        """Log, and store, where one reply's time went. Never fails the scan.

        Only a conversation the reply pipeline ran for is reported: an
        already-answered thread or a burst still being typed was not a reply.
        """
        if not timer.has("reply"):
            return
        status = str(outcome.get("status") or "unknown")
        conversation_key = outcome.get("conversation_id") or outcome.get("conversation") or ""
        logger.info(
            "[TIMING] conversation=%s status=%s burst=%s %s",
            conversation_key, status, timer.burst_size, timer.summary(),
        )
        if self.timings is None:
            return
        try:
            await self.timings.record(
                conversation_key=conversation_key,
                task_id=getattr(task, "id", ""),
                status=status,
                burst_size=timer.burst_size,
                milliseconds=timer.milliseconds(),
            )
        except Exception:
            logger.exception(
                "[TIMING] storing reply timings failed conversation=%s", conversation_key,
            )

    async def _read_and_reply(
        self, task, context, target, fallback_view=None, _pre_send_rechecks=0,
        _initial_view=None, skip_already_replied_check=False, *, timer,
    ):
        """read messages -> unanswered burst -> customer finished? -> reply -> verify.

        The open conversation is the only thing that decides whether this
        customer still needs an answer. A checkpoint resumed after a restart,
        a lost task row or a stale send signature never shortcut this read.
        """
        view = _initial_view or self._conversation_view(context.observation)
        conversation_id = self._conversation_id(context.observation, target)
        pending = view.pending_inbound()
        message = pending[-1] if pending else view.latest_inbound
        published = int(
            context.observation.metadata.get("published_message_events", 0) or 0
        )
        if message is None:
            # The observation can be a state the extension published before the
            # thread finished rendering, which carries no messages at all - or
            # messages whose direction the DOM did not expose. Neither is proof
            # that the customer wrote nothing, so the trusted reader used during
            # the hold gets one fresh read before this thread is given up on.
            fresh = await self._reread_view(context)
            if fresh is not None:
                logger.info(
                    "[INBOX] thread re-read conversation=%s messages=%s "
                    "(the observation carried none)",
                    target, len(fresh.messages),
                )
                view = fresh
                pending = view.pending_inbound()
                message = pending[-1] if pending else view.latest_inbound
        if message is None and fallback_view is not None:
            fallback_pending = fallback_view.pending_inbound()
            fallback_message = (
                fallback_pending[-1]
                if fallback_pending else fallback_view.latest_inbound
            )
            if fallback_message is not None:
                logger.info(
                    "[INBOX] using pre-Accept Message request content "
                    "conversation=%s inbound=%s",
                    target, fallback_message.id,
                )
                view = fallback_view
                pending = fallback_pending
                message = fallback_message
        if message is None and self._nothing_to_answer(view):
            logger.info(
                "[INBOX] nothing to answer conversation=%s (%s); the newest "
                "rendered message is ours",
                target, self._unreadable_reason(view),
            )
            return {
                "conversation": target,
                "conversation_id": conversation_id,
                "status": "nothing_to_answer",
            }
        if message is None:
            logger.warning(
                "[INBOX] no readable inbound message conversation=%s "
                "(%s); %s",
                target, self._unreadable_reason(view),
                (
                    "using the post-Accept resend prompt"
                    if fallback_view is not None
                    else "the conversation was opened and stays for the next scan"
                ),
            )
            self._log_unresolved(context.observation, target)
            return {
                "conversation": target,
                "conversation_id": conversation_id,
                "status": "no_inbound_message",
            }
        if self._is_settled(conversation_id, message):
            # This exact inbound message was already answered in an earlier
            # scan. Re-answering it is what a stale unread marker or a thread
            # that re-rendered would otherwise cause.
            logger.info(
                "[INBOX] inbound=%s conversation=%s already handled; not answered again",
                message.id, target,
            )
            return {
                "conversation": target,
                "conversation_id": conversation_id,
                "status": "already_handled",
                "inbound_message_id": message.id,
                "inbound_fingerprint": self._fingerprint(message),
            }
        if not pending:
            outbound = view.outbound_after(message)
            logger.info(
                "[INBOX] already replied conversation=%s inbound=%s outbound=%s",
                target, message.id, outbound.id if outbound else "none",
            )
            return {
                "conversation": target,
                "conversation_id": conversation_id,
                "status": "already_replied",
                "inbound_message_id": message.id,
                "inbound_fingerprint": self._fingerprint(message),
                "outbound_message_id": outbound.id if outbound else None,
            }

        # Show this message on the dashboard immediately - before the burst
        # quiet-period wait below (up to reply_quiet_period_seconds, ~20s by
        # default) and before any reply is generated. An operator watching
        # Inbox should see a new message the moment it is read off the DOM,
        # not only once the agent has decided how to answer it. Safe to
        # persist this early/partial snapshot: the later persist inside
        # reply_provider.reply() (and the final one below for the human-
        # controlled/no-provider branches) is idempotent - store_visible_
        # messages inserts ON CONFLICT DO NOTHING keyed by content hash - and
        # will pick up anything the customer adds during the wait.
        persist_thread = getattr(self.reply_provider, "persist_thread", None)
        if persist_thread is not None:
            await persist_thread(
                self._build_context(task, context, target, conversation_id, view, message, pending)
            )

        # The customer may still be typing the rest of their question. The
        # burst is answered only once it has stopped growing. The wait happens
        # here, on the open thread, so this customer is finished before the next
        # one is opened.
        finished, waited = self._customer_finished(conversation_id, pending)
        interrupted = ""
        if not finished:
            with timer.phase("hold"):
                (
                    finished, waited, pending, latest_view, interrupted,
                ) = await self._wait_for_customer(
                    context, target, conversation_id, pending
                )
            message = pending[-1] if pending else message
            if latest_view is not None:
                # A message sent during the wait only exists in a *re-read*
                # view - keep using the pre-wait `view` here and the DB
                # persist below silently drops every message after the
                # first one in a multi-message burst (see
                # _wait_for_customer's docstring).
                view = latest_view
        if interrupted:
            # The hold gave up the Chrome session on purpose. What the thread
            # shows is still written down - an operator taking over needs to
            # see the message they are answering - but no reply is generated.
            persist_thread = getattr(self.reply_provider, "persist_thread", None)
            if persist_thread is not None:
                await persist_thread(
                    self._build_context(
                        task, context, target, conversation_id, view, message, pending
                    )
                )
            return {
                "conversation": target,
                "conversation_id": conversation_id,
                # `human_controlled` is settled: the conversation now belongs
                # to a person and must never accumulate agent retries.
                # `yielded_to_priority_task` is the opposite - this customer
                # is still owed an answer, on the next scan - but it must not
                # spend a retry either, since nothing about the thread went
                # wrong. See platforms/tiktok/scan_outcomes.py.
                "status": (
                    "human_controlled" if interrupted == "human_controlled"
                    else "yielded_to_priority_task"
                ),
                "pending_messages": len(pending),
                "inbound_message_id": message.id,
                "inbound_fingerprint": self._fingerprint(message),
            }
        if not finished:
            logger.info(
                "[INBOX] waiting for customer conversation=%s messages=%s "
                "waited=%.1fs quiet_period=%.1fs",
                target, len(pending), waited, self.reply_quiet_period_seconds,
            )
            return {
                "conversation": target,
                "conversation_id": conversation_id,
                "status": "waiting_for_customer",
                "pending_messages": len(pending),
                "inbound_message_id": message.id,
            }
        logger.info(
            "[INBOX] inbound message selected id=%s conversation=%s "
            "text_hash=%s characters=%s",
            message.id, target, content_fingerprint(message.text), len(message.text),
        )
        logger.info(
            "[INBOX] unanswered messages conversation=%s count=%s latest=%s "
            "characters=%s published_events=%s",
            target, len(pending), message.id,
            sum(len(item.text) for item in pending), published,
        )
        self._announce_message(
            "conversation.message.received", conversation_id, target, message,
        )
        timer.burst_size = len(pending)
        conversation = self._build_context(
            task, context, target, conversation_id, view, message, pending, timer=timer,
        )
        if self.conversation_control is not None and not (
            await self.conversation_control.agent_may_reply(conversation_id)
        ):
            # A human owns this conversation: the rendered thread (inbound
            # messages, and anything the human already sent through TikTok
            # directly) is still synced to the database - only reply
            # generation is skipped. Without this, `conversation_messages`
            # never gets written for a human-controlled conversation at all,
            # since that write normally happens inside reply_provider.reply().
            persist_thread = getattr(self.reply_provider, "persist_thread", None)
            if persist_thread is not None:
                await persist_thread(conversation)
            logger.info(
                "[INBOX] conversation is human-controlled; skipping reply "
                "generation conversation=%s",
                target,
            )
            return {
                "conversation": target,
                "conversation_id": conversation_id,
                "status": "human_controlled",
                "inbound_message_id": message.id,
                "inbound_fingerprint": self._fingerprint(message),
            }
        if self.reply_provider is None or self.reply_skill is None:
            # The post-action observation already published the inbound event;
            # the asynchronous message pipeline owns the reply in this setup.
            return {
                "conversation": target,
                "conversation_id": conversation_id,
                "status": "handed_to_pipeline",
                "published_events": published,
                "inbound_message_id": message.id,
                "inbound_fingerprint": self._fingerprint(message),
            }

        logger.info(
            "[LLM] generating reply conversation=%s inbound=%s burst=%s",
            target, message.id, len(pending),
        )
        with timer.phase("reply"):
            decision = await self.reply_provider.reply(conversation)
        if self.conversation_control is not None and not (
            await self.conversation_control.agent_may_reply(conversation_id)
        ):
            # The conversation was taken over by a human while the reply was
            # being generated. The decision is already computed but must
            # never be sent - this is the race-condition guard.
            logger.info(
                "[INBOX] generated reply discarded because conversation is "
                "human controlled conversation=%s",
                target,
            )
            return {
                "conversation": target,
                "conversation_id": conversation_id,
                "status": "human_controlled",
                "inbound_message_id": message.id,
                "inbound_fingerprint": self._fingerprint(message),
            }
        # Opening an escalation must not mean silent customer abandonment.
        # PipelineReplyProvider supplies a safe acknowledgement (should_send
        # stays True even though requires_human is also True) for exactly
        # that reason - but `_hand_over` flips conversation_control away
        # from AUTO, and the policy gate that enforces WAITING_HUMAN/HUMAN
        # (core/policy.py) cannot tell "the courtesy ack the handoff itself
        # is sending" apart from any other agent-origin send. Calling
        # `_hand_over` before that ack's own send would therefore have it
        # block itself. When there is an ack to send, the handoff is
        # deferred until right after that send attempt (both branches
        # below); when there is nothing to send, it still fires immediately,
        # exactly as before.
        handoff_reason = (
            f"reply_withheld:{decision.reason or 'requires_human'}"
            if decision.requires_human else None
        )
        ack_pending = handoff_reason is not None and decision.should_send and (
            decision.text.strip() or decision.attachments
        )
        if handoff_reason is not None and not ack_pending:
            await self._hand_over(
                conversation_id, target, handoff_reason, message, decision.analysis,
            )
        if decision.should_send and (decision.text.strip() or decision.attachments):
            logger.info(
                "[INBOX] reply generated text_hash=%s characters=%s model=%s "
                "conversation=%s intent=%s requested_media=%s attachments=%s",
                content_fingerprint(decision.text), len(decision.text.strip()),
                decision.model or "unknown", target,
                getattr(decision.analysis, "intent", "unknown"),
                list(getattr(decision.analysis, "requested_media", ()) or ()),
                [item.kind for item in decision.attachments],
            )
        if not decision.should_send or (
            not decision.text.strip() and not decision.attachments
        ):
            logger.info(
                "[INBOX] reply withheld conversation=%s reason=%s requires_human=%s",
                target, decision.reason or "no_reply", decision.requires_human,
            )
            return {
                "conversation": target,
                "conversation_id": conversation_id,
                "status": "requires_human" if decision.requires_human else "no_reply",
                "reason": decision.reason,
                "inbound_message_id": message.id,
                "inbound_fingerprint": self._fingerprint(message),
            }

        # Reply generation can take seconds. Re-read the same thread immediately
        # before creating a send task; a newer inbound invalidates the entire
        # decision, including its attachments. This is what makes
        # "Gửi ảnh Milo" followed by "Không cần ảnh nữa" cancel the image.
        verifier = getattr(getattr(context, "platform", None), "state_verifier", None)
        if getattr(verifier, "read_conversation", None) is not None:
            recheck_started = monotonic()
            latest_view = await self._reread_view(context)
            # A conversation can render nothing at all for a short window
            # right after an action changed screens (e.g. just after Accept,
            # while TikTok is still swapping the request UI for the normal
            # thread) - a couple of short retries here separates that from a
            # genuinely broken read, without weakening the "did a new inbound
            # arrive while we were generating?" check below in any way.
            recheck_attempt = 0
            while (
                latest_view is None
                and recheck_attempt < PRE_SEND_RECHECK_RETRIES
            ):
                recheck_attempt += 1
                await asyncio.sleep(PRE_SEND_RECHECK_RETRY_DELAY_SECONDS)
                latest_view = await self._reread_view(context)
            if latest_view is None and fallback_view is not None:
                # Some TikTok message-request-to-chat transitions never
                # re-render the customer's pre-accept message into the
                # normal thread at all - only the "Message request
                # accepted" banner shows, and it stays that way (observed
                # in production: `candidates=1 notices=1` unchanged across
                # 5+ independent re-opens over two minutes, long past any
                # "still rendering" window). There is nothing live to
                # compare a newer inbound against in that case, so the only
                # usable ground truth left is the same pre-accept snapshot
                # the reply was already generated from - not a fresh read,
                # but not a blind guess either, and it is exactly what
                # `pending`/`before_watermark` above were already computed
                # from, so the "did anything change?" check just below
                # naturally finds no difference and proceeds to send.
                logger.warning(
                    "[INBOX] pre-send recheck found no live thread "
                    "conversation=%s; falling back to the pre-accept "
                    "snapshot this reply was generated from instead of "
                    "escalating", target,
                )
                latest_view = fallback_view
            timer.add("recheck", monotonic() - recheck_started)
            if latest_view is None:
                await self._hand_over(
                    conversation_id, target, "pre_send_recheck_unavailable", message
                )
                return {
                    "conversation": target,
                    "conversation_id": conversation_id,
                    "status": "requires_human",
                    "reason": "pre_send_recheck_unavailable",
                    "inbound_message_id": message.id,
                    "inbound_fingerprint": self._fingerprint(message),
                }
            latest_pending = latest_view.pending_inbound()
            before_watermark = self._burst_signature(pending)
            after_watermark = (
                self._burst_signature(latest_pending) if latest_pending else ""
            )
            if after_watermark != before_watermark:
                logger.info(
                    "[INBOX] pre-send inbound changed conversation=%s old=%s new=%s; "
                    "discarding the old ReplyDecision",
                    target, before_watermark, after_watermark or "answered",
                )
                if _pre_send_rechecks >= 2:
                    await self._hand_over(
                        conversation_id, target, "inbound_changed_during_reply_generation",
                        latest_pending[-1] if latest_pending else message,
                    )
                    return {
                        "conversation": target,
                        "conversation_id": conversation_id,
                        "status": "requires_human",
                        "reason": "inbound_changed_during_reply_generation",
                        "inbound_message_id": (
                            latest_pending[-1].id if latest_pending else message.id
                        ),
                    }
                return await self._read_and_reply(
                    task, context, target, fallback_view=fallback_view,
                    _pre_send_rechecks=_pre_send_rechecks + 1,
                    _initial_view=latest_view,
                    skip_already_replied_check=skip_already_replied_check,
                    timer=timer,
                )

        reply_task = Task(
            id=f"{task.id}-reply-{uuid4().hex[:8]}",
            # A reply inherits the account of the scan that found the message.
            account_id=task.account_id,
            capability="reply_to_message",
            platform=task.platform,
            payload={
                # The stable thread key identifies the conversation; the
                # username is what the open-chat header is matched against.
                "conversation_id": conversation_id,
                "recipient": target,
                "response": decision.text.strip(),
                "attachments": [
                    attachment.model_dump(mode="json")
                    for attachment in decision.attachments
                ],
                "confirm_send": True,
                # The inbound message this reply answers: it is part of the
                # send signature and of every "already replied?" check.
                "inbound_message_id": message.id,
                "inbound_watermark": self._burst_signature(pending),
                **(
                    {"skip_already_replied_check": True}
                    if skip_already_replied_check else {}
                ),
                # PolicyEngine's conversation-control guard keys off this,
                # never off capability - a human-submitted task carries
                # "human" instead.
                "origin": "agent",
            },
        )
        logger.info(
            "[INBOX] sending generated reply conversation=%s task_id=%s model=%s",
            target, reply_task.id, decision.model or "unknown",
        )
        # The messaging skill owns focus -> input -> send -> send verification.
        send_started = monotonic()
        result = await self._execute_reply(reply_task, context)
        send_seconds = monotonic() - send_started
        verify_seconds = (result.data or {}).get("verify_seconds")
        if verify_seconds is not None:
            # The messaging skill times its own read-back; the rest of the
            # send task is focusing, typing, staging media and the click.
            verify_seconds = min(float(verify_seconds), send_seconds)
            timer.add("verify", verify_seconds)
            send_seconds -= verify_seconds
        timer.add("send", send_seconds)
        if ack_pending:
            # Deferred from above: the courtesy ack's own send is what had to
            # go out while conversation_control was still AUTO. Now that it
            # has (successfully or not - the underlying tool/analysis
            # problem needs a person regardless), flip the conversation over.
            await self._hand_over(
                conversation_id, target, handoff_reason, message, decision.analysis,
            )
        if result.success and (result.data or {}).get("status") == "already_replied":
            logger.info(
                "[INBOX] conversation already answered before sending conversation=%s",
                target,
            )
            return {
                "conversation": target,
                "conversation_id": conversation_id,
                "status": "already_replied",
                "reply_task_id": reply_task.id,
                "inbound_message_id": message.id,
                "inbound_fingerprint": self._fingerprint(message),
            }
        if result.success:
            logger.info(
                "[VERIFY] reply sent successfully conversation=%s inbound=%s task_id=%s",
                target, message.id, reply_task.id,
            )
            await self._remember_reply(
                context, conversation, decision.text.strip(), decision.attachments,
            )
            self._announce_message(
                "conversation.message.sent", conversation_id, target, message,
            )
            return {
                "conversation": target,
                "conversation_id": conversation_id,
                "status": "sent",
                "reply_task_id": reply_task.id,
                "inbound_message_id": message.id,
                "inbound_fingerprint": self._fingerprint(message),
            }
        if (
            not ack_pending
            and (result.data or {}).get("send_status") == MessageSendState.NOT_SENT.value
        ):
            # Send was never pressed, so the customer has nothing from us and a
            # later attempt cannot duplicate anything. A busy or slow screen is
            # the usual cause; the retry budget bounds how long it is tried.
            logger.warning(
                "[INBOX] reply not sent, retrying on a later scan conversation=%s "
                "error=%s", target, result.error,
            )
            return {
                "conversation": target,
                "conversation_id": conversation_id,
                "status": "send_not_attempted",
                "reply_task_id": reply_task.id,
                "inbound_message_id": message.id,
                "inbound_fingerprint": self._fingerprint(message),
                "error": result.error,
            }
        logger.warning(
            "[INBOX] reply not verified conversation=%s error=%s", target, result.error
        )
        # Send may already have been clicked. Reopening this inbound and asking
        # the model for differently worded text creates a new exact signature
        # and can duplicate a reply, so uncertain delivery requires a person.
        if (result.data or {}).get("reply_visible"):
            # The reply is the newest bubble on screen, only not provably
            # after the inbound. Left unrecorded it is on TikTok but missing
            # from the dashboard, and this conversation - now owed to a
            # person - is not reopened by any later scan to pick it up. The
            # row is marked unverified until a DOM re-read re-keys it.
            await self._remember_reply(
                context, conversation, decision.text.strip(), verified=False,
            )
            self._announce_message(
                "conversation.message.sent", conversation_id, target, message,
            )
        self._hand_over_later(
            conversation_id, target,
            "delivery_uncertain_requires_reconciliation", message,
        )
        return {
            "conversation": target,
            "conversation_id": conversation_id,
            "status": "reconcile_required",
            "reply_task_id": reply_task.id,
            "inbound_message_id": message.id,
            "inbound_fingerprint": self._fingerprint(message),
            "error": result.error,
        }

    async def _remember_reply(
        self, context, conversation, text, attachments=(), *, verified=True,
    ):
        """Write down a reply the send verification proved was delivered.

        Without this the answer is only recorded when a later scan happens to
        re-read the thread, so the last reply of a conversation the customer
        never continues would never be stored. A reply that failed to leave
        the composer is not part of the history; `verified=False` is only for
        one the thread shows as its newest bubble without proving its place.
        """
        record = getattr(self.reply_provider, "record_sent", None)
        if record is None or (not text and not attachments):
            return
        try:
            if verified:
                await record(conversation, text, attachments)
            else:
                await record(conversation, text, attachments, verified=False)
        except Exception:
            logger.exception(
                "[INBOX] storing the sent reply failed conversation=%s",
                conversation.conversation_key,
            )
            return
        # The dashboard's sleep/wake idle sweeper (dashboard_api/sleep_wake.py)
        # reads AgentState.last_business_activity_at to decide a workspace is
        # truly idle. core/graph.py only bumps it when the *top-level* task's
        # capability is a messaging one - but an agent-generated reply runs
        # nested inside the messaging.scan_inbox task (this whole method),
        # never as its own top-level task, so that check alone never fires
        # for agent replies. Without this, a workspace with active, ongoing
        # agent conversations would still fall asleep on schedule as if
        # nothing had happened - the exact bug this call closes.
        state = getattr(context, "state", None)
        mark = getattr(state, "mark_business_activity", None)
        if mark is not None:
            mark()

    async def _execute_reply(self, reply_task, context):
        """Run the messaging skill as the reply task, not as the scan task.

        The reply is a send task of its own: policy, the send-task dedup layer
        and send verification all key on its id, so it is persisted and made the
        current task for the duration of the send.
        """
        repository = getattr(getattr(context, "memory", None), "repository", None)
        ensure_task = getattr(repository, "ensure_task", None)
        if ensure_task is not None:
            await ensure_task(reply_task)
        has_task = hasattr(context, "task")
        previous_task = getattr(context, "task", None) if has_task else None
        if has_task:
            context.task = reply_task
        try:
            result = await self.reply_skill.execute(reply_task, context)
        finally:
            if has_task:
                context.task = previous_task
        complete = getattr(repository, "complete", None)
        if complete is not None:
            await complete(
                reply_task.id, result.success, result.error, result.data
            )
        return result

    @staticmethod
    def _conversation_view(observation):
        """The rendered thread, ordered, with explicit inbound/outbound."""
        return ConversationView.from_observation(observation)

    @staticmethod
    def _nothing_to_answer(view):
        """True when the thread was read fine and holds nothing to answer.

        Only an outbound-only thread with every direction resolved qualifies:
        an empty thread has not finished rendering, and an `UNKNOWN` bubble
        means the DOM did not say who wrote it - both are read failures and
        stay on the technical retry path. A thread whose newest rendered
        message is ours is the opposite: TikTok renders the bottom of a thread
        first, so a customer message arriving later shows up as `pending_
        inbound` on the next scan and is answered then.
        """
        if not view.messages:
            return False
        return all(item.is_outbound for item in view.messages)

    @staticmethod
    def _unreadable_reason(view):
        """Why a thread yielded no inbound message, in one log-friendly phrase.

        "empty thread" and "the DOM never said who wrote these" are different
        failures with different fixes, and they used to log identically.
        """
        if not view.messages:
            return "the thread rendered no messages"
        directions = Counter(item.direction for item in view.messages)
        return (
            f"messages={len(view.messages)} directions="
            + ",".join(f"{name}:{count}" for name, count in sorted(directions.items()))
        )

    @staticmethod
    def _log_unresolved(observation, target):
        """Report every bubble the parser could not place, with its DOM metadata.

        A thread that yields no inbound message is either still rendering or
        parsed wrong; only this block tells the two apart, so it is printed
        instead of the conversation being dropped in silence.
        """
        metadata = getattr(observation, "metadata", None) or {}
        messages = metadata.get("conversation_messages") or (
            (metadata.get("extension_state") or {}).get("messages") or ()
        )
        unresolved = [
            item for item in messages
            if isinstance(item, dict)
            and str(item.get("direction", "UNKNOWN")).upper() == "UNKNOWN"
        ]
        if not unresolved:
            logger.info(
                "[INBOX] conversation=%s carried no rendered message at all "
                "(the thread had not finished rendering)", target,
            )
            return
        for item in unresolved:
            logger.warning(
                "[INBOX][DEBUG] direction unresolved conversation=%s\n%s",
                target, direction_debug_block(item),
            )

    @classmethod
    def _latest_inbound_message(cls, observation):
        """The newest message the customer sent, read from the open thread.

        Only an explicit INBOUND direction counts; screen position is never
        treated as evidence of who wrote a message.
        """
        return cls._conversation_view(observation).latest_inbound

    @classmethod
    def _history(cls, observation):
        return cls._conversation_view(observation).as_history()

    async def _maybe_reload_for_new_activity(self, context):
        """Reload the page when a live signal says something new arrived.

        `platform.list_message_requests` (used when `auto_accept_message_
        requests` is on) reads a queue that TikTok does not refresh on its
        own once the page has been sitting loaded for a while - confirmed
        live: a stranger's first message never showed up there until the
        page was reloaded. Two things do update live and are used here to
        decide when that reload is worth paying for - never a blind timer,
        so a workspace with nothing new to show never reloads, and the
        idle-based auto-sleep decision (`dashboard_api/sleep_wake.py`) is
        never affected by this:

        * `extension_state.activity_unread_count` - the Activity/notification
          badge (the bell icon), already published fresh on every observation
          without any extra action; otherwise never looked at at all
          (`_can_skip_scan`'s own docstring: "an Activity badge is never a
          reason to scan and never a reason to skip").
        * `extension_state.notifications_seen` - a count of real
          `Notification(...)` calls the page itself made, reported by
          `extension/dom/notification_hook.js` (a MAIN-world content script -
          the only place that can see this, since a native notification
          renders outside the page's own DOM entirely). Only meaningful once
          Chrome actually allows TikTok to raise one - see
          `tools/browser.py`'s `ChromeBrowserTool.start()`, which grants that
          permission itself now that `dashboard_api/processes.py` no longer
          launches Chrome with `--disable-notifications`.
        """
        extension_state = (context.observation.metadata or {}).get("extension_state") or {}
        activity = int(extension_state.get("activity_unread_count", 0) or 0)
        notifications = int(extension_state.get("notifications_seen", 0) or 0)
        previous_activity = self._last_activity_count
        previous_notifications = self._last_notifications_seen
        self._last_activity_count = activity
        self._last_notifications_seen = notifications
        if previous_activity is None or previous_notifications is None:
            return  # first observation this process has seen - baseline only
        if activity <= previous_activity and notifications <= previous_notifications:
            return
        # Reload only from a screen with nothing to lose - never mid
        # conversation, where an unsent draft or an in-flight send would be
        # wiped out from under the operator/customer.
        if context.observation.screen not in (TikTokScreen.HOME, TikTokScreen.MESSAGES):
            return
        logger.info(
            "[INBOX] live signal changed (activity %s -> %s, notifications %s -> %s); "
            "reloading so the message-request queue reflects it",
            previous_activity, activity, previous_notifications, notifications,
        )
        await context.execute_action(Action(
            name="platform.reload_page",
            reason="TikTok's message-request queue needs a reload to show a newly arrived request",
        ))

    def _can_skip_scan(self, detected):
        """Skip opening Messages only on a trustworthy negative unread signal.

        The check is deliberately narrow:

        * only extension-sourced state may produce a conclusive negative, since
          a plain DOM snapshot cannot always see the nav badge;
        * if the Messages panel is already open, the conversation rows are
          scanned instead of concluding `no_unread_signal` from the badge;
        * an Activity badge is never a reason to scan and never a reason to
          skip - it is not part of this decision at all.
        """
        # A customer already recorded as unanswered has lost their unread flag
        # by definition, so a negative badge can never suppress that revisit.
        if self._unanswered or not self.trust_negative_unread_badge:
            return False
        tool = detected.data.get("tool") or {}
        if tool.get("messages_panel_visible") or tool.get("conversations_observed"):
            return False
        return tool.get("unread") is False and tool.get("source") == "extension"

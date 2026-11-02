"""One-time historical inbox import for a freshly connected TikTok account.

`InboxScanSkill` (`platforms/tiktok/inbox_watcher.py`) only ever looks at
conversations TikTok currently marks unread - correct for ongoing operation,
but it means a brand new workspace whose TikTok account already has real
conversation history shows an empty Customers/Inbox page until each of those
customers writes again. This skill imports every conversation currently
visible in the panel (not just unread ones) and stores its rendered history
through the same `persist_thread` path `InboxScanSkill` uses for a
human-controlled conversation - never generating or sending a reply, so a
workspace freshly connected to a busy TikTok account cannot suddenly message
dozens of old customers at once.

One execution only ever imports a bounded batch (`batch_size`), well inside
`policy.limits.max_actions_per_task` - an inbox with hundreds of old
conversations would otherwise blow a single task's action budget partway
through and never mark itself finished. Once a batch leaves nothing pending,
this skill submits its own follow-up task through the same task repository
every other skill already uses (`context.memory.repository`), so the account
naturally converges to a full import over a few automatic runs.

What is already imported, and whether the whole account is finished, is
recorded in the same `InboxLedgerRepository` table `InboxScanSkill` uses
(`storage/inbox_ledger.py`), under reserved keys that can never collide with
a real TikTok username: `_conversation_key` casefolds and strips a leading
`@`, so it never produces a key containing `:` or wrapped in `__..__`.
"""

import logging
from uuid import uuid4

from agent.reply_service import ConversationContext
from models.action import Action
from models.result import ActionResult
from models.tasks import Task
from platforms.tiktok.inbox_watcher import InboxScanSkill
from platforms.tiktok.screens import TikTokScreen
from skills.base import Skill


logger = logging.getLogger(__name__)

BACKFILL_MARKER_KEY = "__inbox_backfill_done__"
IMPORTED_KEY_PREFIX = "backfill:"

MESSAGING_SCREENS = frozenset({TikTokScreen.MESSAGES, TikTokScreen.CONVERSATION})

# Enough follow-up batches to clear `max_conversations` even at the smallest
# allowed `batch_size` (1), with headroom for a few conversations that keep
# failing to open along the way - past this, one broken row would otherwise
# resubmit this task forever within the same run.
MAX_CONTINUATIONS = 50


class InboxBackfillSkill(Skill):
    """Imports one bounded batch of existing conversation history, no replies."""

    name = "messaging_inbox_backfill"
    capabilities = ["messaging.backfill_inbox"]

    def __init__(
        self, max_conversations=300, batch_size=20,
        reply_provider=None, ledger=None,
    ):
        # Hard ceiling across every batch combined - conversations beyond it
        # are never seen at all, since `list_all_conversations` itself stops
        # collecting names once it reaches this count.
        self.max_conversations = max(1, min(500, int(max_conversations)))
        # Per-execution cap: ~1.3 actions per conversation (open_conversation,
        # occasionally a re-open_inbox), kept well under
        # policy.limits.max_actions_per_task (200 by default) alongside the
        # listing itself and its own open_inbox.
        self.batch_size = max(1, min(50, int(batch_size)))
        # Attached post-hoc by main.py once the message runtime exists, same
        # as InboxScanSkill.reply_provider - only its persist_thread() is
        # used here, never reply()/send().
        self.reply_provider = reply_provider
        self.ledger = ledger
        self._settled = {}
        self._ledger_loaded = False
        # In-memory only, for this process's lifetime: a conversation that
        # never manages to open (broken DOM row, transient TikTok error) is
        # never marked imported, so `remaining` would otherwise never reach 0
        # and this would resubmit itself forever. A restart resets the count,
        # which is an acceptable trade for not needing this cap to survive
        # one - runaway *resubmission within a single run* is what it exists
        # to stop.
        self._continuations = 0

    async def can_handle(self, task, context):
        return task.capability in self.capabilities

    async def already_done(self) -> bool:
        """Whether this account's historical import has fully finished.

        Checked both here (before the first task is even queued, in
        main.py's `_seed_inbox_backfill`) and at the top of `execute` (in
        case a task was queued just before an earlier batch finished the
        job) - either check finding it done is enough to skip.
        """
        await self._restore_ledger()
        return BACKFILL_MARKER_KEY in self._settled

    @staticmethod
    def _imported_key(name):
        return IMPORTED_KEY_PREFIX + InboxScanSkill._conversation_key(name)

    async def execute(self, task, context):
        if task.platform != "tiktok_web":
            return ActionResult(
                success=False,
                error="The inbox backfill is available only for TikTok Web",
            )
        await self._restore_ledger()
        if BACKFILL_MARKER_KEY in self._settled:
            return ActionResult(success=True, data={"status": "already_done"})

        if context.observation.screen not in MESSAGING_SCREENS:
            opened = await context.execute_action(Action(
                name="platform.open_inbox",
                reason="The historical import starts from the verified Messages surface",
            ))
            if not opened.success:
                return opened

        listed = await context.execute_action(Action(
            name="platform.list_all_conversations",
            args={"limit": self.max_conversations},
            reason="Read every visible conversation, not just unread ones, for the historical import",
        ))
        if not listed.success:
            return listed
        names = list(listed.data.get("tool", {}).get("conversations", []))
        pending = [name for name in names if self._imported_key(name) not in self._settled]
        batch = pending[: self.batch_size]
        logger.info(
            "[INBOX] backfill batch starting found=%s pending=%s batch=%s",
            len(names), len(pending), len(batch),
        )

        imported = 0
        empty = 0
        failed = []
        for name in batch:
            if context.observation.screen not in MESSAGING_SCREENS:
                inbox = await context.execute_action(Action(
                    name="platform.open_inbox",
                    reason="Return to Messages before opening the next conversation",
                ))
                if not inbox.success:
                    failed.append(name)
                    continue
            opened = await context.execute_action(Action(
                name="platform.open_conversation",
                args={"target": name},
                reason="Open one exact existing conversation to import its rendered history",
            ))
            if not opened.success:
                logger.warning(
                    "[INBOX] backfill could not open conversation=%s; skipping", name,
                )
                failed.append(name)
                continue
            view = InboxScanSkill._conversation_view(context.observation)
            if view.messages:
                conversation_id = InboxScanSkill._conversation_id(context.observation, name)
                persist_thread = getattr(self.reply_provider, "persist_thread", None)
                if persist_thread is not None:
                    last = view.messages[-1]
                    await persist_thread(ConversationContext(
                        platform=task.platform,
                        conversation_key=conversation_id,
                        # Unused by persist_thread (it only reads .history);
                        # kept non-empty because the dataclass requires them.
                        message_id=last.id,
                        message_text=last.text,
                        customer_name=name,
                        avatar_url=InboxScanSkill._active_avatar_url(context.observation, name),
                        history=view.as_history(),
                    ))
                imported += 1
            else:
                empty += 1
            await self._mark_imported(name)

        # A name in `batch` that failed to open is never marked imported (see
        # the `continue` above), so it is still owed - not just whatever this
        # batch left unopened beyond `batch_size`. Counting only the latter
        # would let a persistently-failing conversation get silently marked
        # "done" forever the moment everything else in `pending` fit in one
        # batch.
        remaining = (len(pending) - len(batch)) + len(failed)
        if remaining <= 0:
            await self._mark_done()
            logger.info(
                "[INBOX] backfill finished for this account conversations_found=%s",
                len(names),
            )
        else:
            await self._queue_next_batch(task, context)
            logger.info("[INBOX] backfill batch done; %s conversation(s) still pending", remaining)
        return ActionResult(success=True, data={
            "conversations_found": len(names),
            "conversations_imported": imported,
            "conversations_empty": empty,
            "conversations_failed": len(failed),
            "conversations_remaining": max(0, remaining),
        })

    async def _queue_next_batch(self, task, context):
        """Submit a follow-up task so a large inbox finishes over several runs.

        `context.memory.repository` is the same `TaskRepository` every skill's
        graph runs against (`core/memory.py`'s `Memory.repository`) - this is
        not a new capability, just the existing task queue used from inside a
        skill instead of from a scheduler.
        """
        self._continuations += 1
        if self._continuations > MAX_CONTINUATIONS:
            logger.warning(
                "[INBOX] backfill exceeded %s continuations in this run; giving up "
                "for now instead of resubmitting forever - a persistently unopenable "
                "conversation is the likely cause. Restarting the workspace tries again.",
                MAX_CONTINUATIONS,
            )
            return
        repository = getattr(getattr(context, "memory", None), "repository", None)
        if repository is None:
            return
        try:
            if await repository.has_active_capability(task.capability):
                return
            await repository.submit(Task(
                id=f"inbox-backfill-{uuid4()}",
                account_id=task.account_id,
                capability=task.capability,
                platform=task.platform,
                priority=task.priority,
                payload={"automatic": True},
            ))
        except Exception:
            logger.exception("[INBOX] queuing the next backfill batch failed")

    async def _restore_ledger(self):
        if self._ledger_loaded or self.ledger is None:
            return
        self._ledger_loaded = True
        try:
            settled, _unanswered = await self.ledger.load()
        except Exception:
            logger.exception(
                "[INBOX] restoring the backfill ledger failed; treating as not yet run"
            )
            return
        self._settled = settled

    async def _mark_imported(self, name):
        key = self._imported_key(name)
        self._settled[key] = {"status": "backfill_imported"}
        await self._persist(key, name=name, status="backfill_imported")

    async def _mark_done(self):
        self._settled[BACKFILL_MARKER_KEY] = {"status": "backfill_completed"}
        await self._persist(
            BACKFILL_MARKER_KEY, name=BACKFILL_MARKER_KEY, status="backfill_completed",
        )

    async def _persist(self, key, *, name, status):
        if self.ledger is None:
            return
        try:
            await self.ledger.settle(
                key, name=name, status=status,
                inbound_message_id="backfill", fingerprint="",
            )
        except Exception:
            logger.exception("[INBOX] persisting backfill progress failed key=%s", key)

"""Credential-free tests for the scenarios not yet covered by
tests/test_reply_dedup_flow.py and tests/test_inbox_scan_flow.py:

- a recipient switch mid-flow (not just before the first click) never sends
  to the wrong customer (CS-164, severity CRITICAL in the CS test plan)
- a media rejection with no safe fallback never reports SENT (CS-165/166)
- many independent conversations processed concurrently never cross-
  contaminate each other's reply context (CS-167/168/169/177)
- a long single-conversation burst is answered once, in order (CS-170)
- customer facts and messages never leak across conversation_id or
  account_id (CS-172/173/176)

No model is called anywhere; reply text/decisions are passed in directly,
exactly like the inbox scan does after its `ReplyProvider` decided.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.reply_service import ReplyDecision
from models.message_delivery import MessageSendState
from models.observation import Observation
from models.result import ActionResult
from models.tasks import Task
from platforms.tiktok.inbox_watcher import InboxScanSkill
from platforms.tiktok.messaging import ReplyMessageSkill
from platforms.tiktok.screens import TikTokScreen
from storage.conversation_context import ConversationContextRepository
from tests.support import make_database


def message(identifier, text, direction, index):
    return {"id": identifier, "text": text, "direction": direction, "index": index}


def observation(messages, sequence=10, active="customer",
                 send_state=MessageSendState.UNCERTAIN):
    return Observation(
        screen=TikTokScreen.CONVERSATION,
        confidence=0.97,
        perception_source="extension",
        auth_state="LOGGED_IN",
        message_send_state=send_state,
        elements=[{"resource_id": "dm-new-chat-nickname", "text": active}],
        metadata={"extension_state": {"messages": list(messages), "sequence": sequence}},
    )


def reply_task(task_id="reply-1", recipient="customer", inbound="m3", text="Dạ còn hàng ạ"):
    return Task(
        account_id="acc_test",
        id=task_id, capability="reply_to_message", platform="tiktok_web",
        payload={
            "conversation_id": recipient, "recipient": recipient,
            "response": text, "confirm_send": True, "inbound_message_id": inbound,
        },
    )


INBOUND_ONLY = [
    message("m1", "Chào shop", "INBOUND", 0),
    message("m2", "Xin chào", "OUTBOUND", 1),
    message("m3", "Còn hàng không?", "INBOUND", 2),
]
ANSWERED = INBOUND_ONLY + [message("m4", "Dạ còn hàng ạ", "OUTBOUND", 3)]


# ============================================================ GROUP Z (extra)
class MidFlowRecipientSwitchTests(unittest.IsolatedAsyncioTestCase):
    """CS-164/CS-178: the active conversation changes *after* typing has
    already started, not only before the first click. `_verify_recipient_
    before_send` is re-read fresh from the DOM before every draft mutation
    (focus, each attachment, and again immediately before Send) - this proves
    the *last* one, right before Send, is what actually gates the click, not
    a cached decision from when the flow started."""

    class SwitchingContext:
        """The header reads `first_recipient` for the early checks and
        silently becomes `switched_to` starting on the Nth fresh read - the
        same shape a real UI switch mid-task would produce."""

        def __init__(self, first_recipient, switched_to, switch_after_reads):
            self.observation = observation(INBOUND_ONLY, active=first_recipient)
            self.actions = []
            self._reads = 0
            self._first = first_recipient
            self._switched = switched_to
            self._switch_after = switch_after_reads
            self.memory = SimpleNamespace(repository=None)
            self.platform = SimpleNamespace(
                state_verifier=SimpleNamespace(read_conversation=self._read_conversation)
            )

        async def _read_conversation(self):
            self._reads += 1
            active = (
                self._switched if self._reads > self._switch_after else self._first
            )
            # Mirrors whatever `self.observation` currently carries, so a
            # post-send confirmation reading through this same seam sees the
            # landed reply exactly like the pre-send recipient check does.
            current_messages = self.observation.metadata["extension_state"]["messages"]
            return SimpleNamespace(
                active_chat=active, active_chat_present=True,
                composer_present=True, conversations=(), messages=current_messages,
            )

        async def execute_action(self, action):
            self.actions.append(action)
            if action.name == "browser.input_text":
                # Typing itself always succeeds; only the *recipient* changes.
                pass
            return ActionResult(success=True)

    async def test_switch_after_focus_but_before_send_aborts_without_sending(self):
        # 1 fresh read happens before focus (passes as "customer"); the
        # switch to "other_customer" is visible on every read after that, so
        # the pre-attachment/pre-send re-check must catch it.
        context = self.SwitchingContext(
            first_recipient="customer", switched_to="other_customer",
            switch_after_reads=1,
        )
        result = await ReplyMessageSkill().execute(
            reply_task(recipient="customer"), context,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.data["status"], "recipient_mismatch")
        action_names = [action.name for action in context.actions]
        self.assertNotIn("platform.send_message", action_names)
        self.assertIn("platform.clear_message_draft", action_names)

    async def test_switch_exactly_at_the_final_pre_send_check_still_aborts(self):
        # Every read before this one still reports the original customer -
        # only the very last check, immediately before Send, sees the switch.
        context = self.SwitchingContext(
            first_recipient="customer", switched_to="other_customer",
            switch_after_reads=2,
        )
        result = await ReplyMessageSkill().execute(
            reply_task(recipient="customer"), context,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.data["status"], "recipient_mismatch")
        self.assertNotIn(
            "platform.send_message", [action.name for action in context.actions]
        )

    async def test_no_switch_at_all_sends_normally(self):
        """Control case: proves the harness itself is not what blocks the
        send - only an actual switch does."""
        context = self.SwitchingContext(
            first_recipient="customer", switched_to="customer",
            switch_after_reads=999,
        )

        class SendingContext(self.SwitchingContext):
            async def execute_action(self, action):
                await super().execute_action(action)
                if action.name == "platform.send_message":
                    self.observation = observation(
                        ANSWERED, sequence=11, active="customer",
                        send_state=MessageSendState.SENT,
                    )
                return ActionResult(success=True)

        context = SendingContext(
            first_recipient="customer", switched_to="customer",
            switch_after_reads=999,
        )
        result = await ReplyMessageSkill().execute(
            reply_task(recipient="customer"), context,
        )
        self.assertTrue(result.success, result.error)
        self.assertIn(
            "platform.send_message", [action.name for action in context.actions]
        )


# =========================================================== GROUP AA (extra)
class MediaRejectionNoFallbackTests(unittest.IsolatedAsyncioTestCase):
    """CS-165/166: a rejection TikTok's UI gives that does not match any of
    the known safe-fallback markers (`_safe_text_only_fallback`) must abort
    the whole reply rather than silently guessing it is safe to continue
    text-only. Never reports SENT; the draft is cleared either way."""

    class RejectingContext:
        def __init__(self, error):
            self.observation = observation(INBOUND_ONLY)
            self.actions = []
            self._error = error
            self.memory = SimpleNamespace(repository=None)
            self.platform = SimpleNamespace(
                state_verifier=SimpleNamespace(read_conversation=self._read_conversation)
            )

        async def _read_conversation(self):
            current_messages = self.observation.metadata["extension_state"]["messages"]
            return SimpleNamespace(
                active_chat="customer", active_chat_present=True,
                composer_present=True, conversations=(), messages=current_messages,
            )

        async def execute_action(self, action):
            self.actions.append(action)
            if action.name == "platform.attach_message_media":
                return ActionResult(success=False, error=self._error)
            return ActionResult(success=True)

    async def test_unrecognized_rejection_reason_aborts_instead_of_guessing_safe(self):
        # Not one of the whitelisted attachment-rejection phrases in
        # `_safe_text_only_fallback` - e.g. a transient network error, which
        # says nothing about whether the media itself was actually rejected.
        context = self.RejectingContext(error="network timeout while uploading")
        task = reply_task(text="Dạ sản phẩm hiện còn hàng ạ")
        task.payload["attachments"] = [{
            "kind": "image", "path": "/catalogue/front.jpg", "name": "front.jpg",
        }]

        result = await ReplyMessageSkill().execute(task, context)

        self.assertFalse(result.success)
        actions = [action.name for action in context.actions]
        self.assertNotIn("platform.send_message", actions)
        self.assertIn("platform.clear_message_draft", actions)
        self.assertNotEqual(result.data.get("status"), "sent")

    async def test_recognized_attachment_rejection_falls_back_to_text_only_and_sends(self):
        """Control case: the whitelisted path still works (already covered in
        test_reply_dedup_flow.py); repeated here so the two error strings are
        compared side by side in one file."""
        landed = INBOUND_ONLY + [message(
            "out-text", "Dạ sản phẩm hiện còn hàng ạ", "OUTBOUND", 3
        )]

        class Context(self.RejectingContext):
            async def execute_action(self, action):
                self.actions.append(action)
                if action.name == "platform.attach_message_media":
                    return ActionResult(
                        success=False,
                        error="TikTok rejected the attachment: invalid media",
                    )
                if action.name == "platform.send_message":
                    self.observation = observation(
                        landed, sequence=11, send_state=MessageSendState.SENT,
                    )
                return ActionResult(success=True)

        context = Context(error="unused")
        task = reply_task(text="Dạ sản phẩm hiện còn hàng ạ")
        task.payload["attachments"] = [{
            "kind": "image", "path": "/catalogue/front.jpg", "name": "front.jpg",
        }]

        result = await ReplyMessageSkill().execute(task, context)

        self.assertTrue(result.success, result.error)
        self.assertIn(
            "platform.send_message", [action.name for action in context.actions]
        )


# ================================================================= GROUP AB
class ConcurrentConversationIsolationTests(unittest.IsolatedAsyncioTestCase):
    """CS-167/168/169/177: N independent conversations handled concurrently
    on one `InboxScanSkill` instance (the shared `_settled`/`_pending`
    dictionaries are exactly where cross-contamination would show up) must
    each get exactly their own message text and their own recipient - never
    another conversation's."""

    class RecordingProvider:
        """Records the exact context it was called with and answers with
        that same conversation's key baked into the reply text, so a mix-up
        is visible in the assertion instead of only in a side channel."""

        def __init__(self):
            self.contexts = []
            self.lock = asyncio.Lock()

        async def reply(self, context):
            async with self.lock:
                self.contexts.append(context)
            # A tiny yield lets other concurrent calls interleave here,
            # which is exactly the window a shared-state bug would need.
            await asyncio.sleep(0)
            return ReplyDecision(
                text=f"reply-for-{context.conversation_key}",
                should_send=True, model="stub",
            )

    class RecordingReplySkill:
        """Records which task it was asked to send, never touches the DOM."""

        def __init__(self):
            self.tasks = []

        async def execute(self, task, context):
            self.tasks.append(task)
            return ActionResult(
                success=True, data={"decision": "complete_task", "send_status": "SENT"}
            )

    async def test_twenty_concurrent_conversations_never_swap_context_or_recipient(self):
        provider = self.RecordingProvider()
        reply_skill = self.RecordingReplySkill()
        skill = InboxScanSkill(reply_provider=provider, reply_skill=reply_skill)

        count = 20
        targets = [f"customer_{i:02d}" for i in range(count)]

        async def run_one(target):
            view_messages = [
                message(f"{target}-m1", f"unique question from {target}", "INBOUND", 0),
            ]
            context = SimpleNamespace(
                observation=observation(view_messages, active=target),
                platform=SimpleNamespace(state_verifier=None),
            )
            return target, await skill._handle_conversation(
                Task(account_id="acc_test", id=f"scan-{target}",
                     capability="messaging.scan_inbox", platform="tiktok_web"),
                context, target,
            )

        results = dict(await asyncio.gather(*(run_one(t) for t in targets)))

        # Every conversation was actually seen by the provider exactly once.
        self.assertEqual(len(provider.contexts), count)
        seen_keys = {ctx.conversation_key for ctx in provider.contexts}
        self.assertEqual(seen_keys, set(targets))
        for ctx in provider.contexts:
            # The message text handed to the model belongs to this exact
            # conversation and no other - the failure mode this guards
            # against is target B's text ending up in target A's context.
            self.assertIn(ctx.conversation_key, ctx.message_text)
            self.assertNotIn(
                "unique question from", ctx.message_text.replace(
                    f"unique question from {ctx.conversation_key}", ""
                ),
            )
        for target, outcome in results.items():
            self.assertEqual(outcome["conversation"], target)
            self.assertEqual(outcome["status"], "sent")
        # Each send task carried exactly its own recipient - never another
        # concurrently-running conversation's.
        sent_recipients = {task.payload["recipient"] for task in reply_skill.tasks}
        self.assertEqual(sent_recipients, set(targets))
        self.assertEqual(len(reply_skill.tasks), count)


# ================================================================= GROUP AC
class LongSingleConversationBurstTests(unittest.IsolatedAsyncioTestCase):
    """CS-170: 50 rapid messages in one conversation must become exactly one
    reply task, covering the whole burst - not 50 separate sends - and the
    order the customer actually typed in must survive into the model's
    input, unshuffled."""

    async def test_fifty_message_burst_is_answered_once_in_order(self):
        burst = [
            message(f"m{i}", f"message number {i}", "INBOUND", i)
            for i in range(50)
        ]
        context = SimpleNamespace(
            observation=observation(burst, active="customer"),
            platform=SimpleNamespace(state_verifier=None),
        )

        class RecordingProvider:
            def __init__(self):
                self.calls = 0
                self.seen_text = None

            async def reply(self, ctx):
                self.calls += 1
                self.seen_text = ctx.message_text
                return ReplyDecision(
                    text="Dạ shop đã nhận đủ thông tin ạ", should_send=True, model="stub",
                )

        provider = RecordingProvider()
        sent = []

        class RecordingReplySkill:
            async def execute(self, task, ctx):
                sent.append(task.payload["response"])
                return ActionResult(
                    success=True, data={"decision": "complete_task", "send_status": "SENT"}
                )

        skill = InboxScanSkill(
            reply_provider=provider, reply_skill=RecordingReplySkill(),
        )

        outcome = await skill._handle_conversation(
            Task(account_id="acc_test", id="scan-burst",
                 capability="messaging.scan_inbox", platform="tiktok_web"),
            context, "customer",
        )

        self.assertEqual(provider.calls, 1)
        self.assertEqual(len(sent), 1)
        self.assertEqual(outcome["status"], "sent")
        lines = provider.seen_text.splitlines()
        self.assertEqual(len(lines), 50)
        self.assertEqual(lines, [f"message number {i}" for i in range(50)])


# ================================================================= GROUP AE
class PrivacyIsolationTests(unittest.IsolatedAsyncioTestCase):
    """CS-172/173/176: one customer's phone/address/messages must never be
    readable from another conversation_id, and never across accounts."""

    async def test_facts_never_leak_between_two_conversations_same_account(self):
        database = make_database(self)
        repo = ConversationContextRepository(database, minimum_fact_confidence=0.5)
        repo.register_conversation("conv-A")
        repo.register_conversation("conv-B")

        await repo.merge_facts(
            "conv-A", {"phone": "0912345678", "address": "10 ABC, Hà Nội"},
            source_message_id="mA1", confidence=0.9,
        )

        facts_a = await repo.get_customer_facts("conv-A")
        facts_b = await repo.get_customer_facts("conv-B")

        self.assertEqual(facts_a["phone"]["value"], "0912345678")
        self.assertEqual(facts_b, {})

    async def test_messages_never_leak_between_two_conversations_same_account(self):
        database = make_database(self)
        repo = ConversationContextRepository(database)
        repo.register_conversation("conv-A")
        repo.register_conversation("conv-B")

        await repo.store_visible_messages("conv-A", [
            SimpleNamespace(direction="INBOUND", text="địa chỉ của tôi là 10 ABC"),
        ], customer_id="cust-a")
        await repo.store_visible_messages("conv-B", [
            SimpleNamespace(direction="INBOUND", text="chào shop"),
        ], customer_id="cust-b")

        recent_a = await repo.load_recent_messages("conv-A")
        recent_b = await repo.load_recent_messages("conv-B")

        self.assertEqual(len(recent_a), 1)
        self.assertIn("10 ABC", recent_a[0]["text"])
        self.assertEqual(len(recent_b), 1)
        self.assertNotIn("10 ABC", recent_b[0]["text"])

    async def test_facts_never_leak_across_two_different_accounts(self):
        database_a = make_database(self, account_id="acc_a")
        database_b = make_database(self, account_id="acc_b")
        repo_a = ConversationContextRepository(database_a)
        repo_b = ConversationContextRepository(database_b)
        # The same conversation_id string can legitimately occur in both
        # accounts (two independent TikTok shops), which is exactly the case
        # that must stay isolated.
        repo_a.register_conversation("shared-conv")
        repo_b.register_conversation("shared-conv")

        await repo_a.merge_facts(
            "shared-conv", {"phone": "0911111111"},
            source_message_id="a1", confidence=0.9,
        )

        facts_a = await repo_a.get_customer_facts("shared-conv")
        facts_b = await repo_b.get_customer_facts("shared-conv")

        self.assertEqual(facts_a["phone"]["value"], "0911111111")
        self.assertEqual(facts_b, {})


if __name__ == "__main__":
    unittest.main()

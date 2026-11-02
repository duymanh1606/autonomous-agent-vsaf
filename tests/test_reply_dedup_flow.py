"""Credential-free tests for the DOM-truth reply flow and the send dedup layer.

No model is called anywhere: reply text is passed in directly, exactly like the
inbox scan does after its `ReplyProvider` decided.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.conversation_view import ConversationView
from perception.state_parser import StateParser
from models.message_delivery import (
    MessageSendState,
    SendTaskState,
    message_signature,
)
from models.observation import Observation
from models.result import ActionResult
from models.tasks import Task
from platforms.tiktok.messaging import ReplyMessageSkill
from platforms.tiktok.screens import TikTokScreen
from tests.support import make_database
from storage.repositories import TaskRepository


BASE_DIR = Path(__file__).resolve().parents[1]


def message(identifier, text, direction, index, timestamp=None):
    return {
        "id": identifier, "text": text, "direction": direction,
        "index": index, "timestamp": timestamp,
    }


INBOUND_ONLY = [
    message("m1", "Chào shop", "INBOUND", 0),
    message("m2", "Xin chào", "OUTBOUND", 1),
    message("m3", "Còn hàng không?", "INBOUND", 2),
]
ANSWERED = INBOUND_ONLY + [message("m4", "Dạ còn hàng ạ", "OUTBOUND", 3)]


def observation(screen=TikTokScreen.CONVERSATION, messages=INBOUND_ONLY,
                send_state=MessageSendState.UNCERTAIN, sequence=10):
    return Observation(
        screen=screen,
        confidence=0.97,
        perception_source="extension",
        auth_state="LOGGED_IN",
        message_send_state=send_state,
        elements=[{"resource_id": "dm-new-chat-nickname", "text": "customer"}],
        metadata={"extension_state": {"messages": list(messages), "sequence": sequence}},
    )


class StubObserver:
    """Reobserves as the real DOM/vision observer would, minus any DOM: the
    UNCERTAIN wait-then-reobserve branch finds no new evidence, matching how
    these tests behaved before that branch existed."""

    def __init__(self, context):
        self._context = context

    async def observe(self, **kwargs):
        return self._context.observation


class StubContext:
    """Records actions; the observation swaps exactly like the real graph."""

    def __init__(self, repository=None, after_send=None, lands_on="platform.send_message"):
        self.observation = observation()
        self.actions = []
        self.after_send = after_send
        # The action after which the thread shows the message: the text Send
        # normally, the media dialog's Send for a file sent on its own.
        self.lands_on = lands_on
        self.memory = type("Memory", (), {"repository": repository})()
        self.task = None
        self.observer = StubObserver(self)
        self.state = type("State", (), {"update_observation": lambda self, obs: None})()

    async def execute_action(self, action):
        self.actions.append(action)
        if action.name == "browser.input_text" and not str(action.args.get("text") or "").strip():
            # As the real browser does (tools/browser.py input_message). A
            # stub that accepted "" once hid that a file sent on its own
            # could never be sent at all.
            return ActionResult(success=False, error="Message must contain 1-1000 printable characters")
        if action.name == self.lands_on and self.after_send is not None:
            self.observation = self.after_send
        return ActionResult(success=True)


def reply_task(task_id="reply-1", inbound="m3", text="Dạ còn hàng ạ"):
    return Task(
        account_id="acc_test",
        id=task_id, capability="reply_to_message", platform="tiktok_web",
        payload={
            "conversation_id": "customer", "recipient": "customer",
            "response": text, "confirm_send": True, "inbound_message_id": inbound,
        },
    )


class ConversationViewTests(unittest.TestCase):
    def test_state_parser_preserves_extension_media_only_messages(self):
        parsed = StateParser._extension_messages({"messages": [{
            "id": "out-photo", "text": "", "direction": "OUTBOUND",
            "media": [{"type": "image", "src": "blob:photo"}],
        }]})
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].media[0]["type"], "image")
        self.assertTrue(parsed[0].id)

    def test_media_only_outbound_is_kept_and_can_verify_delivery(self):
        view = ConversationView.from_items([
            {"id": "in-1", "text": "Cho xem ảnh", "direction": "INBOUND", "index": 0},
            {"id": "out-1", "text": "", "direction": "OUTBOUND", "index": 1,
             "media": [{"type": "image", "src": "blob:photo"}]},
        ])
        self.assertEqual(len(view), 2)
        self.assertEqual(view.outbound_media_after(view.latest_inbound).id, "out-1")

    def test_media_verification_requires_the_requested_kind(self):
        view = ConversationView.from_items([
            {"id": "in-1", "text": "Cho xem video", "direction": "INBOUND", "index": 0},
            {"id": "out-1", "text": "", "direction": "OUTBOUND", "index": 1,
             "media": [{"kind": "image", "src": "blob:photo"}]},
            {"id": "out-2", "text": "", "direction": "OUTBOUND", "index": 2,
             "media": [{"kind": "video", "src": "blob:video"}]},
        ])
        inbound = view.latest_inbound
        self.assertEqual(view.outbound_media_after(inbound, kind="image").id, "out-1")
        self.assertEqual(view.outbound_media_after(inbound, kind="video").id, "out-2")

    def test_latest_inbound_is_the_newest_customer_message(self):
        view = ConversationView.from_items(INBOUND_ONLY)
        self.assertEqual(view.latest_inbound.id, "m3")
        self.assertIsNone(view.outbound_after(view.latest_inbound))
        self.assertFalse(view.already_replied())

    def test_outbound_after_latest_inbound_means_answered(self):
        view = ConversationView.from_items(ANSWERED)
        self.assertTrue(view.already_replied())
        self.assertEqual(view.outbound_after(view.latest_inbound).id, "m4")

    def test_an_older_outbound_never_counts_as_a_reply(self):
        view = ConversationView.from_items(INBOUND_ONLY)
        # "Xin chào" was sent before the last question and must not silence it.
        self.assertIsNone(view.outbound_after(view.latest_inbound))

    def test_timestamps_decide_when_they_disagree_with_nothing_else(self):
        view = ConversationView.from_items([
            message("a", "hỏi", "INBOUND", 0, timestamp=1_700_000_000),
            message("b", "đáp", "OUTBOUND", 1, timestamp=1_700_000_060),
        ])
        self.assertTrue(view.already_replied())

    def test_dom_order_is_enough_without_any_timestamp(self):
        view = ConversationView.from_items([
            {"id": "a", "text": "hỏi", "direction": "INBOUND"},
            {"id": "b", "text": "đáp", "direction": "OUTBOUND"},
        ])
        self.assertTrue(view.already_replied())


class SendSignatureTests(unittest.TestCase):
    def test_signature_covers_conversation_inbound_and_reply_text(self):
        base = message_signature("tiktok_web", "customer", "m3", "Dạ còn hàng ạ")
        self.assertEqual(
            base, message_signature("tiktok_web", "@Customer", "m3", "Dạ còn  hàng ạ")
        )
        self.assertNotEqual(
            base, message_signature("tiktok_web", "customer", "m9", "Dạ còn hàng ạ")
        )
        self.assertNotEqual(
            base, message_signature("tiktok_web", "other", "m3", "Dạ còn hàng ạ")
        )
        self.assertNotEqual(
            base, message_signature("tiktok_web", "customer", "m3", "Hết hàng ạ")
        )


class PreSendRecipientTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_fresh_header_is_rejected_without_database(self):
        async def wrong_thread():
            return SimpleNamespace(
                active_chat="other_customer", active_chat_present=True,
                composer_present=True, conversations=(), messages=(),
            )

        context = SimpleNamespace(
            platform=SimpleNamespace(
                state_verifier=SimpleNamespace(read_conversation=wrong_thread)
            )
        )
        allowed, reason = await ReplyMessageSkill()._verify_recipient_before_send(
            context, "customer", "thread-customer"
        )
        self.assertFalse(allowed)
        self.assertIn("header", reason)

    async def test_wrong_recipient_clears_draft_and_never_types_or_sends(self):
        async def wrong_thread():
            return SimpleNamespace(
                active_chat="other_customer", active_chat_present=True,
                composer_present=True, conversations=(), messages=(),
            )

        class Context:
            def __init__(self):
                self.observation = observation()
                self.platform = SimpleNamespace(
                    state_verifier=SimpleNamespace(read_conversation=wrong_thread)
                )
                self.memory = SimpleNamespace(repository=None)
                self.actions = []

            async def execute_action(self, action):
                self.actions.append(action.name)
                return ActionResult(success=True)

        context = Context()
        result = await ReplyMessageSkill().execute(reply_task(), context)

        self.assertFalse(result.success)
        self.assertEqual(result.data["status"], "recipient_mismatch")
        self.assertEqual(context.actions, ["platform.clear_message_draft"])

    async def test_retry_does_not_attach_media_kind_already_visible_after_inbound(self):
        partial = INBOUND_ONLY + [{
            "id": "out-image", "text": "", "direction": "OUTBOUND", "index": 3,
            "media": [{"kind": "image", "src": "blob:image"}],
        }]
        landed = partial + [message(
            "out-text", "Dạ sản phẩm hiện còn hàng ạ", "OUTBOUND", 4
        )]
        context = StubContext(after_send=observation(
            messages=landed, send_state=MessageSendState.SENT, sequence=11
        ))
        context.observation = observation(messages=partial)
        task = reply_task(text="Dạ sản phẩm hiện còn hàng ạ")
        task.payload["attachments"] = [{
            "kind": "image", "path": "/catalogue/front.jpg", "name": "front.jpg",
        }]

        result = await ReplyMessageSkill().execute(task, context)

        self.assertTrue(result.success, result.error)
        self.assertNotIn(
            "platform.attach_message_media", [action.name for action in context.actions]
        )

    async def test_explicit_media_rejection_can_fall_back_to_safe_text_only(self):
        landed = INBOUND_ONLY + [message(
            "out-text", "Dạ sản phẩm hiện còn hàng ạ", "OUTBOUND", 3
        )]

        class Context(StubContext):
            async def execute_action(self, action):
                self.actions.append(action)
                if action.name == "platform.attach_message_media":
                    return ActionResult(
                        success=False, error="TikTok rejected the attachment: invalid media"
                    )
                if action.name == "platform.send_message":
                    self.observation = observation(
                        messages=landed,
                        send_state=MessageSendState.SENT,
                        sequence=11,
                    )
                return ActionResult(success=True)

        context = Context(repository=None)
        task = reply_task(text="Dạ sản phẩm hiện còn hàng ạ")
        task.payload["attachments"] = [{
            "kind": "image", "path": "/catalogue/front.jpg", "name": "front.jpg",
        }]

        result = await ReplyMessageSkill().execute(task, context)

        self.assertTrue(result.success, result.error)
        actions = [action.name for action in context.actions]
        self.assertEqual(actions.count("platform.attach_message_media"), 1)
        self.assertEqual(actions.count("platform.clear_message_draft"), 1)
        self.assertEqual(actions.count("browser.input_text"), 2)
        self.assertEqual(actions.count("platform.send_message"), 1)


class SendTaskDedupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repository = TaskRepository(make_database(self))
        self.signature = message_signature("tiktok_web", "customer", "m3", "hi")

    async def claim(self, task_id, **kwargs):
        return await self.repository.claim_send_task(
            signature=self.signature, task_id=task_id, platform="tiktok_web",
            conversation_id="customer", inbound_message_id="m3", **kwargs
        )

    async def test_first_claim_is_granted_and_a_second_task_is_blocked(self):
        self.assertTrue((await self.claim("task-1")).granted)
        second = await self.claim("task-2")
        self.assertFalse(second.granted)
        self.assertTrue(second.in_flight)
        self.assertEqual(second.reason, "in_flight")

    async def test_sent_blocks_every_further_claim(self):
        await self.claim("task-1")
        await self.repository.mark_send_task(self.signature, SendTaskState.SENT)
        blocked = await self.claim("task-2")
        self.assertFalse(blocked.granted)
        self.assertTrue(blocked.already_sent)

    async def test_failed_and_aborted_allow_a_retry(self):
        # FAILED/ABORTED prove the send never reached the customer (a clean
        # failure before or during the attempt), so a direct retry is safe.
        for state in (SendTaskState.FAILED, SendTaskState.ABORTED):
            await self.claim("task-1")
            await self.repository.mark_send_task(self.signature, state)
            retry = await self.claim("task-2")
            self.assertTrue(retry.granted, state)
            self.assertEqual(retry.reason, f"retry_after_{state.value.casefold()}")

    async def test_expired_requires_reconciliation_before_retry(self):
        # EXPIRED means a worker died mid-send: whether the message actually
        # reached the customer is unknown, so - unlike FAILED/ABORTED - it
        # must not be retried blindly. It joins the same
        # expired_requires_reconciliation gate as a stale in-flight claim.
        await self.claim("task-1")
        await self.repository.mark_send_task(self.signature, SendTaskState.EXPIRED)
        retry = await self.claim("task-2")
        self.assertFalse(retry.granted)
        self.assertEqual(retry.reason, "expired_requires_reconciliation")

    async def test_stale_in_flight_is_taken_over(self):
        await self.claim("task-1")
        taken = await self.claim("task-2", stale_after_seconds=0)
        self.assertFalse(taken.granted)
        self.assertEqual(taken.reason, "expired_requires_reconciliation")

    async def test_restart_expires_in_flight_rows(self):
        await self.claim("task-1")
        self.assertEqual(self.repository.expire_interrupted_send_tasks(), 1)
        resumed = await self.claim("task-1")
        self.assertFalse(resumed.granted)
        self.assertEqual(resumed.reason, "expired_requires_reconciliation")

    async def test_the_same_task_may_resume_its_own_attempt(self):
        await self.claim("task-1")
        resumed = await self.claim("task-1")
        self.assertTrue(resumed.granted)
        self.assertEqual(resumed.reason, "resumed_own_attempt")


class ReplySkillDomTruthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repository = TaskRepository(make_database(self))
        self.skill = ReplyMessageSkill(input_action="browser.input_text")
        # execute() defaults inbound_watermark to inbound_message_id whenever
        # the payload does not carry one explicitly (see ReplyMessageSkill.
        # execute), so the signature here must include it too or every
        # "conflict with an existing claim" test below silently computes a
        # different signature and never actually conflicts with anything.
        self.signature = message_signature(
            "tiktok_web", "customer", "m3", "Dạ còn hàng ạ", inbound_watermark="m3"
        )

    def context(self, **kwargs):
        return StubContext(repository=self.repository, **kwargs)

    async def test_pre_send_recipient_mismatch_is_rejected(self):
        async def wrong_thread():
            return SimpleNamespace(
                active_chat="other_customer", active_chat_present=True,
                composer_present=True, conversations=(), messages=(),
            )

        context = SimpleNamespace(
            platform=SimpleNamespace(
                state_verifier=SimpleNamespace(read_conversation=wrong_thread)
            )
        )
        allowed, reason = await self.skill._verify_recipient_before_send(
            context, "customer", "thread-customer"
        )
        self.assertFalse(allowed)
        self.assertIn("header", reason)

    async def test_conversation_already_answered_is_skipped_before_any_send(self):
        context = self.context()
        context.observation = observation(messages=ANSWERED)
        result = await self.skill.execute(reply_task(), context)
        self.assertTrue(result.success)
        self.assertEqual(result.data["status"], "already_replied")
        self.assertEqual(result.data["outbound_message_id"], "m4")
        self.assertNotIn(
            "platform.send_message", [action.name for action in context.actions]
        )

    async def test_unanswered_inbound_is_sent_and_verified_by_the_thread(self):
        context = self.context(after_send=observation(
            messages=ANSWERED, send_state=MessageSendState.SENT, sequence=11
        ))
        result = await self.skill.execute(reply_task(), context)
        self.assertTrue(result.success, result.error)
        self.assertTrue(result.data["verified"])
        self.assertEqual(result.data["outbound_message_id"], "m4")
        row = await self.repository.get_send_task(self.signature)
        self.assertEqual(row["state"], SendTaskState.SENT.value)

    async def test_image_and_video_are_both_staged_and_verified(self):
        task = reply_task(task_id="reply-both-media", text="Dạ em gửi ảnh và video ạ")
        task.payload["attachments"] = [
            {"kind": "image", "path": "/catalogue/front.jpg", "name": "front.jpg"},
            {"kind": "video", "path": "/catalogue/intro.mp4", "name": "intro.mp4"},
        ]
        landed = INBOUND_ONLY + [
            message("out-image", "", "OUTBOUND", 3) | {
                "media": [{"kind": "image", "src": "blob:image"}],
            },
            message("out-video", "", "OUTBOUND", 4) | {
                "media": [{"kind": "video", "src": "blob:video"}],
            },
            message("out-text", "Dạ em gửi ảnh và video ạ", "OUTBOUND", 5),
        ]
        context = self.context(after_send=observation(
            messages=landed, send_state=MessageSendState.SENT, sequence=11
        ))

        result = await self.skill.execute(task, context)

        self.assertTrue(result.success, result.error)
        actions = [action.name for action in context.actions]
        self.assertEqual(actions.count("platform.attach_message_media"), 2)
        self.assertLess(
            actions.index("platform.attach_message_media"),
            actions.index("platform.send_message"),
        )
        media_paths = [
            action.args["path"] for action in context.actions
            if action.name == "platform.attach_message_media"
        ]
        self.assertEqual(media_paths, [
            "/catalogue/front.jpg", "/catalogue/intro.mp4",
        ])
        self.assertEqual(result.data["outbound_message_id"], "out-video")

    async def test_send_without_a_new_outbound_is_not_reported_as_sent(self):
        context = self.context(after_send=observation(
            messages=INBOUND_ONLY, send_state=MessageSendState.SENT, sequence=11
        ))
        result = await self.skill.execute(reply_task(), context)
        self.assertFalse(result.success)
        self.assertFalse(result.data["verified"])
        row = await self.repository.get_send_task(self.signature)
        # EXPIRED, not FAILED: Send was already clicked, so an unverified
        # outcome may actually have landed. A future retry must reconcile
        # against the open thread first instead of resending blindly.
        self.assertEqual(row["state"], SendTaskState.EXPIRED.value)

    async def test_attachment_only_reply_with_no_caption_is_sent_and_verified(self):
        """An empty `response` with verified attachments is a deliberate
        media-only reply (and what an operator's photo without a caption is).
        TikTok's media dialog sends the file; there is no text to type or to
        send after it. Typing "" failed the whole task before the file was
        staged (reported from a production log), and a text Send with an
        empty composer has nothing to send. It is verified the way any reply
        with a file is: the file visible in the thread after the inbound."""
        task = reply_task(task_id="reply-media-only", text="")
        task.payload["attachments"] = [
            {"kind": "image", "path": "/catalogue/front.jpg", "name": "front.jpg"},
        ]
        landed = INBOUND_ONLY + [
            message("out-image", "", "OUTBOUND", 3) | {
                "media": [{"kind": "image", "src": "blob:image"}],
            },
        ]
        context = StubContext(
            self.repository,
            after_send=observation(messages=landed, send_state=MessageSendState.SENT, sequence=11),
            lands_on="platform.attach_message_media",
        )

        result = await self.skill.execute(task, context)

        self.assertTrue(result.success, result.error)
        self.assertTrue(result.data["verified"])
        self.assertEqual(result.data["outbound_message_id"], "out-image")
        actions = [action.name for action in context.actions]
        self.assertIn("platform.attach_message_media", actions)
        self.assertNotIn("browser.input_text", actions)
        self.assertNotIn("platform.send_message", actions)

    async def test_attachment_only_reply_already_sent_is_skipped_not_resent(self):
        """The pre-send "already replied?" dedup check must also recognise a
        previously landed media-only reply, not just treat every empty
        response as "never sent"."""
        task = reply_task(task_id="reply-media-only-2", text="")
        task.payload["attachments"] = [
            {"kind": "image", "path": "/catalogue/front.jpg", "name": "front.jpg"},
        ]
        context = self.context()
        context.observation = observation(messages=INBOUND_ONLY + [
            message("out-image", "", "OUTBOUND", 3) | {
                "media": [{"kind": "image", "src": "blob:image"}],
            },
        ])

        result = await self.skill.execute(task, context)

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.data["status"], "already_replied")
        self.assertEqual(result.data["outbound_message_id"], "out-image")
        self.assertNotIn(
            "platform.send_message", [action.name for action in context.actions]
        )

    async def test_signature_conflict_with_a_visible_reply_is_marked_handled(self):
        await self.repository.claim_send_task(
            signature=self.signature, task_id="older-task", platform="tiktok_web",
            conversation_id="customer", inbound_message_id="m3",
        )
        context = self.context()
        context.observation = observation(messages=ANSWERED)
        result = await self.skill.execute(reply_task(), context)
        self.assertTrue(result.success)
        self.assertEqual(result.data["status"], "already_replied")
        self.assertNotIn(
            "platform.send_message", [action.name for action in context.actions]
        )

    async def test_signature_conflict_with_a_failed_task_retries_the_send(self):
        await self.repository.claim_send_task(
            signature=self.signature, task_id="older-task", platform="tiktok_web",
            conversation_id="customer", inbound_message_id="m3",
        )
        await self.repository.mark_send_task(self.signature, SendTaskState.FAILED)
        context = self.context(after_send=observation(
            messages=ANSWERED, send_state=MessageSendState.SENT, sequence=11
        ))
        result = await self.skill.execute(reply_task("reply-2"), context)
        self.assertTrue(result.success, result.error)
        self.assertIn(
            "platform.send_message", [action.name for action in context.actions]
        )

    async def test_fresh_in_flight_task_blocks_a_duplicate_send(self):
        await self.repository.claim_send_task(
            signature=self.signature, task_id="older-task", platform="tiktok_web",
            conversation_id="customer", inbound_message_id="m3",
        )
        context = self.context()
        result = await self.skill.execute(reply_task("reply-2"), context)
        self.assertFalse(result.success)
        self.assertEqual(result.data["status"], "send_in_flight")
        self.assertNotIn(
            "platform.send_message", [action.name for action in context.actions]
        )

    async def test_sent_signature_without_dom_evidence_is_escalated_not_resent(self):
        await self.repository.claim_send_task(
            signature=self.signature, task_id="older-task", platform="tiktok_web",
            conversation_id="customer", inbound_message_id="m3",
        )
        await self.repository.mark_send_task(self.signature, SendTaskState.SENT)
        context = self.context()
        result = await self.skill.execute(reply_task("reply-2"), context)
        self.assertFalse(result.success)
        self.assertEqual(result.data["status"], "sent_without_dom_evidence")
        self.assertNotIn(
            "platform.send_message", [action.name for action in context.actions]
        )


class InboxScanDomTruthTests(unittest.IsolatedAsyncioTestCase):
    """The scan itself skips a conversation the DOM shows as answered."""

    class ScanContext:
        def __init__(self, opened):
            self.observation = observation(screen=TikTokScreen.MESSAGES, messages=[])
            self._opened = opened
            self.actions = []

        async def execute_action(self, action):
            self.actions.append(action.name)
            if action.name == "platform.list_unread_conversations":
                return ActionResult(
                    success=True, data={"tool": {"conversations": ["customer"]}}
                )
            if action.name == "platform.open_unread_conversation":
                self.observation = self._opened
            return ActionResult(success=True, data={"tool": {"success": True}})

    async def test_answered_conversation_is_marked_handled_without_replying(self):
        from platforms.tiktok.inbox_watcher import InboxScanSkill

        provider = type("Provider", (), {
            "reply": staticmethod(lambda context: unittest.TestCase().fail(
                "an answered conversation must never reach reply generation"
            )),
        })()
        skill = InboxScanSkill(reply_provider=provider, reply_skill=object())
        context = self.ScanContext(observation(messages=ANSWERED))
        result = await skill.execute(
            Task(account_id="acc_test", id="scan-1", capability="messaging.scan_inbox", platform="tiktok_web"),
            context,
        )
        self.assertTrue(result.success)
        handled = result.data["conversations_handled"][0]
        self.assertEqual(handled["status"], "already_replied")
        self.assertEqual(handled["outbound_message_id"], "m4")

    async def test_unanswered_conversation_passes_the_inbound_id_to_the_reply(self):
        from agent.reply_service import ReplyDecision
        from platforms.tiktok.inbox_watcher import InboxScanSkill

        class Provider:
            async def reply(self, context):
                return ReplyDecision(
                    text="Dạ còn hàng ạ", should_send=True, model="stub"
                )

        class RecordingSkill:
            def __init__(self):
                self.tasks = []

            async def execute(self, task, context):
                self.tasks.append(task)
                return ActionResult(success=True, data={"send_status": "SENT"})

        reply_skill = RecordingSkill()
        skill = InboxScanSkill(reply_provider=Provider(), reply_skill=reply_skill)
        context = self.ScanContext(observation(messages=INBOUND_ONLY))
        result = await skill.execute(
            Task(account_id="acc_test", id="scan-2", capability="messaging.scan_inbox", platform="tiktok_web"),
            context,
        )
        self.assertEqual(result.data["replies_sent"], 1)
        self.assertEqual(reply_skill.tasks[0].payload["inbound_message_id"], "m3")


if __name__ == "__main__":
    unittest.main()

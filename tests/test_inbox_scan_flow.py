"""Credential-free tests for the `messaging.scan_inbox` flow.

No API key is used anywhere: reply generation is exercised through a stub
`ReplyProvider`, exactly the seam `agent.reply_service` exists for.
"""

import asyncio
import sys
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.reply_service import ReplyDecision
from core.state_verifier import StateVerifier
from models.conversation_view import ConversationView
from models.observation import Observation
from models.outbound_message import Attachment
from models.result import ActionResult
from models.tasks import Task
from perception.browser_observer import BrowserObserver
from perception.dom_observer import DomObserver
from perception.state_parser import StateParser
from perception.vision_fallback import VisionFallback
from platforms.tiktok.inbox_watcher import InboxScanSkill
from platforms.tiktok.screens import TikTokScreen
from platforms.tiktok_web.adapter import TikTokWebAdapter


def extension_state(ui_state, sequence, *, mode=None, conversations=(), messages=(),
                    composer=True, confidence=0.97):
    return {
        "ui_state": ui_state,
        "sequence": sequence,
        "page_confidence": confidence,
        "messages_mode": mode,
        "messages_panel": {"present": mode is not None, "visible": mode is not None},
        "active_chat": {
            "present": ui_state == "CONVERSATION",
            "username": "customer" if ui_state == "CONVERSATION" else "",
        },
        "conversations": list(conversations),
        "messages": list(messages),
        "thread": {
            "container": "message_list",
            "nodes": len(messages),
            "notices": 0,
        },
        "composer": {"present": composer, "text": ""},
        "send_button": {"present": composer, "enabled": composer},
        "elements": [],
    }


def trusted_thread_state(messages):
    """Minimal state returned by a verifier that scoped the active thread."""
    return SimpleNamespace(
        messages=list(messages),
        thread_trusted=True,
        thread_source="message_list",
    )


def snapshot(state=None, url="https://www.tiktok.com/"):
    return {
        "url": url,
        "title": "TikTok",
        "body_text": "",
        "signature": "sig",
        "elements": [],
        "session_cookie_names": ["sessionid"],
        "authenticated_dom": True,
        "login_form_visible": False,
        "page_loading": False,
        "extension_state": state,
    }


class StubBrowser:
    """Minimal browser surface: a scripted snapshot and a never-firing event."""

    def __init__(self, snapshots, event_received=False):
        self._snapshots = list(snapshots)
        self.event_received = event_received

    async def wait_for_perception_event(self, timeout_ms, previous_sequence=None,
                                        expected_screen=None):
        return self.event_received

    async def snapshot(self):
        return self._snapshots[0] if len(self._snapshots) == 1 else self._snapshots.pop(0)

    async def set_visual_targets(self, *args):
        return None


class StaleStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_sequence_greater_than_previous_is_new_state_without_event(self):
        """A crossed barrier is fresh even when the event wait timed out."""
        browser = StubBrowser(
            [snapshot(extension_state("MESSAGES", 152, mode="panel"))],
            event_received=False,
        )
        observation = await BrowserObserver(
            browser, VisionFallback(enabled=False), extension_event_wait_ms=1
        ).observe(after_action="platform.open_inbox", previous_extension_sequence=151)
        self.assertEqual(observation.screen, TikTokScreen.MESSAGES)
        self.assertEqual(observation.perception_source, "extension")

    async def test_sequence_may_skip_values(self):
        """`previous + 1` is never required; only `> previous`."""
        browser = StubBrowser([snapshot(extension_state("CONVERSATION", 200, mode="panel"))])
        observation = await BrowserObserver(
            browser, VisionFallback(enabled=False), extension_event_wait_ms=1
        ).observe(
            after_action="platform.open_unread_conversation",
            previous_extension_sequence=151,
        )
        self.assertEqual(observation.screen, TikTokScreen.CONVERSATION)

    async def test_unchanged_sequence_is_stale_and_falls_back_to_dom(self):
        browser = StubBrowser(
            [snapshot(
                extension_state("CONVERSATION", 151, mode="panel"),
                url="https://www.tiktok.com/",
            )],
            event_received=True,
        )
        observation = await BrowserObserver(
            browser, VisionFallback(enabled=False), extension_event_wait_ms=1
        ).observe(
            after_action="platform.open_inbox", previous_extension_sequence=151
        )
        self.assertEqual(observation.perception_source, "dom")
        self.assertEqual(observation.screen, TikTokScreen.FEED)

    async def test_extension_state_outranks_browser_dom_fallback(self):
        """The URL says `/` (feed); the extension proves the drawer is open."""
        browser = StubBrowser([snapshot(
            extension_state("MESSAGES", 9, mode="panel", confidence=0.4),
            url="https://www.tiktok.com/",
        )])
        observation = await BrowserObserver(
            browser, VisionFallback(enabled=False), extension_event_wait_ms=1
        ).observe()
        self.assertEqual(observation.screen, TikTokScreen.MESSAGES)

    async def test_untrusted_raw_thread_messages_never_enter_the_observation(self):
        state = extension_state(
            "CONVERSATION", 10, mode="panel",
            messages=[{
                "id": "bad", "text": "feed text", "direction": "INBOUND",
            }],
        )
        state["thread"]["container"] = "document"
        browser = StubBrowser([snapshot(state)])

        observation = await BrowserObserver(
            browser, VisionFallback(enabled=False), extension_event_wait_ms=1,
        ).observe()

        self.assertEqual(observation.metadata["conversation_messages"], [])


class ExtensionMessageIdentityTests(unittest.TestCase):
    def test_generic_dom_keys_are_not_treated_as_native_message_ids(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "extension" / "platforms" / "tiktok.js"
        ).read_text(encoding="utf-8")
        message_id_function = source.split(
            "function messageId", 1
        )[1].split("function messageTimestamp", 1)[0]
        self.assertIn('attr(node, "data-message-id")', message_id_function)
        self.assertIn('attr(node, "data-msg-id")', message_id_function)
        self.assertNotIn('attr(node, "data-id")', message_id_function)
        self.assertNotIn('attr(node, "data-key")', message_id_function)

    def test_parser_never_scans_document_for_message_candidates(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "extension" / "platforms" / "tiktok.js"
        ).read_text(encoding="utf-8")
        self.assertNotIn("candidatesIn(document)", source)
        self.assertNotIn('source: "document"', source)


class ThreadSourceTrustTests(unittest.TestCase):
    def test_document_thread_is_rejected_before_messages_reach_consumers(self):
        state = extension_state(
            "CONVERSATION", 10, mode="panel",
            messages=[{"id": "bad", "text": "feed text", "direction": "INBOUND"}],
        )
        state["thread"]["container"] = "document"

        parsed = StateParser.parse(snapshot(state))

        self.assertFalse(parsed.thread_trusted)
        self.assertEqual(parsed.thread_source, "document")
        self.assertEqual(parsed.messages, ())

    def test_chat_box_thread_is_allowed(self):
        state = extension_state(
            "CONVERSATION", 10, mode="panel",
            messages=[{"id": "m1", "text": "xin chào", "direction": "INBOUND"}],
        )
        state["thread"]["container"] = "chat_box"

        parsed = StateParser.parse(snapshot(state))

        self.assertTrue(parsed.thread_trusted)
        self.assertEqual(parsed.thread_source, "chat_box")
        self.assertEqual([item.id for item in parsed.messages], ["m1"])


class StubDomObserver(DomObserver):
    def __init__(self, snapshots):
        super().__init__(browser=None, state_parser=StateParser(), default_timeout_ms=0)
        self._snapshots = list(snapshots)

    async def snapshot(self):
        return self._snapshots[0] if len(self._snapshots) == 1 else self._snapshots.pop(0)


class OpenInboxIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_inbox_succeeds_without_clicking_when_panel_conversation_is_open(self):
        dom = StubDomObserver([snapshot(
            extension_state("CONVERSATION", 151, mode="panel", conversations=[
                {"username": "customer", "unread": False},
            ]),
        )])
        adapter = TikTokWebAdapter(browser=None, observer=None, dom_observer=dom)
        result = await adapter.open_inbox()
        self.assertTrue(result["success"])
        self.assertTrue(result["already_open"])
        self.assertEqual(result["screen"], TikTokScreen.CONVERSATION)

    async def test_open_inbox_succeeds_when_messages_panel_is_open(self):
        dom = StubDomObserver([snapshot(extension_state("MESSAGES", 20, mode="panel"))])
        adapter = TikTokWebAdapter(browser=None, observer=None, dom_observer=dom)
        self.assertTrue((await adapter.open_inbox())["already_open"])

    def test_verify_accepts_conversation_for_open_inbox(self):
        adapter = TikTokWebAdapter(browser=None, observer=None)
        observation = Observation(
            screen=TikTokScreen.CONVERSATION,
            confidence=0.97,
            perception_source="extension",
            auth_state="LOGGED_IN",
        )
        from models.action import Action
        self.assertTrue(adapter.verify(Action(name="platform.open_inbox"), None, observation))


class SequenceVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_conversation_confirmation_rejects_wrong_active_chat(self):
        state = extension_state("CONVERSATION", 152, mode="panel")
        state["active_chat"] = {"present": True, "username": "other_customer"}
        dom = StubDomObserver([snapshot(state)])
        verifier = StateVerifier(platform=None, dom_observer=dom, timeout_ms=0)

        confirmed, observed = await verifier.confirm_conversation(
            "customer", previous_sequence=151
        )

        self.assertFalse(confirmed)
        self.assertEqual(observed.active_chat, "other_customer")

    async def test_conversation_confirmation_requires_exact_active_chat(self):
        dom = StubDomObserver([
            snapshot(extension_state("CONVERSATION", 152, mode="panel"))
        ])
        verifier = StateVerifier(platform=None, dom_observer=dom, timeout_ms=0)

        confirmed, _ = await verifier.confirm_conversation(
            "@Customer", previous_sequence=151
        )

        self.assertTrue(confirmed)

    async def test_ui_state_confirmed_by_newer_sequence_and_expected_screen(self):
        dom = StubDomObserver([snapshot(extension_state("CONVERSATION", 152, mode="panel"))])
        verifier = StateVerifier(platform=None, dom_observer=dom, timeout_ms=0)
        confirmed, state = await verifier.confirm_ui_state(
            ["messages", "conversation"], previous_sequence=151
        )
        self.assertTrue(confirmed)
        self.assertEqual(state.screen, TikTokScreen.CONVERSATION)

    async def test_ui_state_rejected_when_sequence_did_not_move(self):
        dom = StubDomObserver([snapshot(extension_state("MESSAGES", 151, mode="panel"))])
        verifier = StateVerifier(platform=None, dom_observer=dom, timeout_ms=0)
        confirmed, _ = await verifier.confirm_ui_state("messages", previous_sequence=151)
        self.assertFalse(confirmed)

    async def test_ui_state_ignores_url_and_accepts_the_panel(self):
        dom = StubDomObserver([snapshot(
            extension_state("MESSAGES", 300, mode="panel"),
            url="https://www.tiktok.com/",
        )])
        verifier = StateVerifier(platform=None, dom_observer=dom, timeout_ms=0)
        confirmed, _ = await verifier.confirm_ui_state(
            ["messages", "conversation"], previous_sequence=10
        )
        self.assertTrue(confirmed)


class StubContext:
    """Records actions and replays scripted results plus the resulting screen."""

    def __init__(self, observation, results):
        self.observation = observation
        self.results = results
        self.actions = []

    async def execute_action(self, action):
        self.actions.append(action.name)
        handler = self.results[action.name]
        result = handler(action) if callable(handler) else handler
        if isinstance(result, tuple):
            result, self.observation = result
        return result


def observation_for(
    screen, messages=(), published=0, active="", rows=(),
    activity_unread=0, notifications_seen=0,
):
    return Observation(
        screen=screen,
        confidence=0.97,
        perception_source="extension",
        auth_state="LOGGED_IN",
        metadata={
            "extension_state": {
                "messages": list(messages),
                "active_chat": {"present": bool(active), "username": active},
                "conversations": list(rows),
                "activity_unread_count": activity_unread,
                "notifications_seen": notifications_seen,
            },
            "published_message_events": published,
        },
    )


class StubReplyProvider:
    def __init__(self, decision):
        self.decision = decision
        self.contexts = []
        self.recorded = []
        self.recorded_unverified = []

    async def reply(self, context):
        self.contexts.append(context)
        return self.decision

    async def record_sent(self, context, text, attachments=(), verified=True):
        entry = (context.conversation_key, text, tuple(attachments))
        (self.recorded if verified else self.recorded_unverified).append(entry)


class StubReplySkill:
    def __init__(self, result):
        self.result = result
        self.tasks = []

    async def execute(self, task, context):
        self.tasks.append(task)
        return self.result


def scan_task():
    return Task(account_id="acc_test", id="inbox-scan-1", capability="messaging.scan_inbox", platform="tiktok_web")


class InboxScanStateRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_conversation_state_scans_panel_without_reopening_inbox(self):
        context = StubContext(
            observation_for(TikTokScreen.CONVERSATION),
            {
                "platform.list_unread_conversations": ActionResult(
                    success=True, data={"tool": {"conversations": []}}
                ),
            },
        )
        result = await InboxScanSkill().execute(scan_task(), context)
        self.assertTrue(result.success)
        self.assertEqual(result.data["reason"], "conversation_panel_no_unread")
        self.assertNotIn("platform.open_inbox", context.actions)
        self.assertNotIn("platform.detect_unread", context.actions)

    async def test_messages_state_scans_without_badge_detection(self):
        context = StubContext(
            observation_for(TikTokScreen.MESSAGES),
            {
                "platform.list_unread_conversations": ActionResult(
                    success=True, data={"tool": {"conversations": []}}
                ),
            },
        )
        result = await InboxScanSkill().execute(scan_task(), context)
        self.assertTrue(result.success)
        self.assertEqual(context.actions, ["platform.list_unread_conversations"])

    async def test_home_state_reads_badge_then_opens_inbox(self):
        context = StubContext(
            observation_for(TikTokScreen.FEED),
            {
                "platform.detect_unread": ActionResult(success=True, data={"tool": {
                    "source": "extension", "unread": True, "unread_count": 1,
                }}),
                "platform.open_inbox": (
                    ActionResult(success=True, data={"tool": {"success": True}}),
                    observation_for(TikTokScreen.MESSAGES),
                ),
                "platform.list_unread_conversations": ActionResult(
                    success=True, data={"tool": {"conversations": []}}
                ),
            },
        )
        result = await InboxScanSkill().execute(scan_task(), context)
        self.assertTrue(result.success)
        self.assertEqual(context.actions, [
            "platform.detect_unread",
            "platform.open_inbox",
            "platform.list_unread_conversations",
        ])

    async def test_home_state_with_no_badge_skips_the_scan(self):
        context = StubContext(
            observation_for(TikTokScreen.FEED),
            {
                "platform.detect_unread": ActionResult(success=True, data={"tool": {
                    "source": "extension", "unread": False,
                }}),
            },
        )
        result = await InboxScanSkill(
            trust_negative_unread_badge=True
        ).execute(scan_task(), context)
        self.assertTrue(result.success)
        self.assertEqual(result.data["reason"], "no_unread_signal")
        self.assertEqual(context.actions, ["platform.detect_unread"])

    async def test_default_rescan_opens_panel_despite_negative_badge(self):
        """A delayed/stale badge must not hide a real unread row."""
        opened = observation_for(
            TikTokScreen.CONVERSATION,
            messages=[{"id": "new-1", "text": "Shop ơi", "direction": "INBOUND"}],
            active="new-customer",
        )
        context = StubContext(
            observation_for(TikTokScreen.FEED),
            {
                "platform.detect_unread": ActionResult(success=True, data={"tool": {
                    "source": "extension", "unread": False, "unread_count": 0,
                }}),
                "platform.open_inbox": (
                    ActionResult(success=True, data={"tool": {"success": True}}),
                    observation_for(TikTokScreen.MESSAGES),
                ),
                "platform.list_unread_conversations": ActionResult(
                    success=True,
                    data={"tool": {"conversations": ["new-customer"]}},
                ),
                "platform.open_unread_conversation": (
                    ActionResult(success=True, data={"tool": {"success": True}}),
                    opened,
                ),
            },
        )
        provider = StubReplyProvider(ReplyDecision(
            text="Dạ shop đây ạ", should_send=True, model="stub"
        ))
        reply = StubReplySkill(ActionResult(
            success=True, data={"decision": "complete_task", "send_status": "SENT"}
        ))

        result = await InboxScanSkill(
            reply_provider=provider, reply_skill=reply
        ).execute(scan_task(), context)

        self.assertEqual(result.data["replies_sent"], 1)
        self.assertEqual(len(provider.contexts), 1)
        self.assertEqual(provider.contexts[0].conversation_key, "new-customer")
        self.assertEqual(context.actions[:3], [
            "platform.detect_unread",
            "platform.open_inbox",
            "platform.list_unread_conversations",
        ])


class InboxScanReplyFlowTests(unittest.IsolatedAsyncioTestCase):
    def _context(self):
        opened = observation_for(
            TikTokScreen.CONVERSATION,
            messages=[
                {"id": "m1", "text": "Chào shop", "direction": "INBOUND"},
                {"id": "m2", "text": "Xin chào", "direction": "OUTBOUND"},
                {"id": "m3", "text": "Còn hàng không?", "direction": "INBOUND"},
            ],
            published=1,
        )
        return StubContext(
            observation_for(TikTokScreen.MESSAGES),
            {
                "platform.list_unread_conversations": ActionResult(
                    success=True, data={"tool": {"conversations": ["customer"]}}
                ),
                "platform.open_unread_conversation": (
                    ActionResult(success=True, data={"tool": {"success": True}}),
                    opened,
                ),
            },
        )

    async def test_opened_conversation_continues_to_reply_and_verified_send(self):
        provider = StubReplyProvider(ReplyDecision(
            text="Dạ còn hàng ạ", should_send=True, reason="generated", model="stub"
        ))
        reply_skill = StubReplySkill(ActionResult(success=True, data={
            "decision": "complete_task", "send_status": "SENT",
        }))
        skill = InboxScanSkill(reply_provider=provider, reply_skill=reply_skill)

        context = self._context()
        result = await skill.execute(scan_task(), context)

        self.assertTrue(result.success)
        self.assertEqual(result.data["conversations_observed"], 1)
        self.assertEqual(result.data["replies_sent"], 1)
        self.assertEqual(result.data["conversations_handled"][0]["status"], "sent")
        # The latest INBOUND message is what was analysed, not the outbound one.
        self.assertEqual(provider.contexts[0].message_text, "Còn hàng không?")
        self.assertEqual(provider.contexts[0].message_id, "m3")
        self.assertEqual(len(provider.contexts[0].history), 3)
        # The send goes through the messaging skill, which owns send verification.
        self.assertEqual(reply_skill.tasks[0].capability, "reply_to_message")
        self.assertEqual(reply_skill.tasks[0].payload["response"], "Dạ còn hàng ạ")
        self.assertTrue(reply_skill.tasks[0].payload["confirm_send"])

    async def test_message_request_is_accepted_acknowledged_and_answered_by_llm(self):
        provider = StubReplyProvider(ReplyDecision(
            text="Dạ shop tư vấn ngay ạ", should_send=True,
            reason="generated", model="stub",
        ))
        reply_skill = StubReplySkill(ActionResult(success=True, data={
            "decision": "complete_task", "send_status": "SENT",
        }))
        request_thread = observation_for(
            TikTokScreen.CONVERSATION,
            messages=[{
                "id": "request-1", "text": "Shop tư vấn sữa giúp mình",
                "direction": "INBOUND",
            }],
            published=1,
            active="new_customer",
        )
        context = StubContext(
            observation_for(TikTokScreen.MESSAGES),
            {
                "platform.detect_message_requests": ActionResult(
                    success=True, data={"tool": {"present": True, "count": 1}}
                ),
                "platform.list_unread_conversations": ActionResult(
                    success=True, data={"tool": {"conversations": []}}
                ),
                "platform.open_message_requests": ActionResult(
                    success=True, data={"tool": {"success": True}}
                ),
                "platform.list_message_requests": ActionResult(
                    success=True,
                    data={"tool": {"conversations": ["new_customer"]}},
                ),
                "platform.open_message_request": (
                    ActionResult(success=True, data={"tool": {"success": True}}),
                    request_thread,
                ),
                "platform.accept_message_request": (
                    ActionResult(success=True, data={"tool": {"success": True}}),
                    request_thread,
                ),
                "platform.return_to_primary_inbox": (
                    ActionResult(success=True, data={"tool": {"success": True}}),
                    observation_for(TikTokScreen.MESSAGES),
                ),
            },
        )
        skill = InboxScanSkill(
            reply_provider=provider,
            reply_skill=reply_skill,
            auto_accept_message_requests=True,
            ledger=StubLedger(),
        )

        result = await skill.execute(scan_task(), context)

        self.assertTrue(result.success)
        self.assertEqual(result.data["message_requests_detected"], 1)
        self.assertEqual(result.data["message_requests_handled"], 1)
        self.assertEqual(result.data["replies_sent"], 1)
        self.assertTrue(result.data["conversations_handled"][0]["message_request"])
        self.assertEqual(result.data["conversations_handled"][0]["status"], "sent")
        # The stranger's pre-accept message is answered for real by the LLM
        # pipeline, using the pre-accept content as its input.
        self.assertEqual(len(provider.contexts), 1)
        self.assertEqual(provider.contexts[0].message_text, "Shop tư vấn sữa giúp mình")
        # Two outbound sends: the fixed courtesy line first, then the real
        # LLM-generated answer, both to the same accepted stranger.
        self.assertEqual(len(reply_skill.tasks), 2)
        self.assertEqual(
            reply_skill.tasks[0].payload["response"],
            "Dạ shop đã nhận được tin nhắn, mình kiểm tra và phản hồi ngay ạ.",
        )
        self.assertEqual(
            reply_skill.tasks[1].payload["response"], "Dạ shop tư vấn ngay ạ",
        )
        # The real answer must bypass the generic "already replied?" guard,
        # since the courtesy line just sent is itself an outbound after the
        # same inbound message.
        self.assertTrue(reply_skill.tasks[1].payload["skip_already_replied_check"])
        self.assertEqual(context.actions, [
            "platform.detect_message_requests",
            "platform.list_unread_conversations",
            "platform.open_message_requests",
            "platform.list_message_requests",
            "platform.open_message_request",
            "platform.accept_message_request",
            "platform.return_to_primary_inbox",
        ])
        # Already inside Messages: the inbox is never reopened mid-loop.
        self.assertNotIn("platform.open_inbox", context.actions)

    async def test_already_settled_message_request_is_not_re_accepted(self):
        """Reproduces a real production log: TikTok kept listing an already-
        accepted request every ~20s scan, so the agent re-opened it, clicked
        Accept again (a risk=high action) and re-read the whole thread every
        single cycle just to rediscover "already handled" at the end. The
        settled check must fire from the preview, before Accept is clicked
        again, once a scan has already answered this exact inbound message."""
        provider = StubReplyProvider(ReplyDecision(
            text="Dạ shop tư vấn ngay ạ", should_send=True,
            reason="generated", model="stub",
        ))
        reply_skill = StubReplySkill(ActionResult(success=True, data={
            "decision": "complete_task", "send_status": "SENT",
        }))
        request_thread = observation_for(
            TikTokScreen.CONVERSATION,
            messages=[{
                "id": "request-1", "text": "Shop tư vấn sữa giúp mình",
                "direction": "INBOUND",
            }],
            published=1,
            active="new_customer",
        )

        def make_results():
            return {
                "platform.detect_message_requests": ActionResult(
                    success=True, data={"tool": {"present": True, "count": 1}}
                ),
                "platform.list_unread_conversations": ActionResult(
                    success=True, data={"tool": {"conversations": []}}
                ),
                "platform.open_message_requests": ActionResult(
                    success=True, data={"tool": {"success": True}}
                ),
                "platform.list_message_requests": ActionResult(
                    success=True,
                    data={"tool": {"conversations": ["new_customer"]}},
                ),
                "platform.open_message_request": (
                    ActionResult(success=True, data={"tool": {"success": True}}),
                    request_thread,
                ),
                "platform.accept_message_request": (
                    ActionResult(success=True, data={"tool": {"success": True}}),
                    request_thread,
                ),
                "platform.return_to_primary_inbox": (
                    ActionResult(success=True, data={"tool": {"success": True}}),
                    observation_for(TikTokScreen.MESSAGES),
                ),
            }

        skill = InboxScanSkill(
            reply_provider=provider,
            reply_skill=reply_skill,
            auto_accept_message_requests=True,
            ledger=StubLedger(),
        )

        first = await skill.execute(
            scan_task(), StubContext(observation_for(TikTokScreen.MESSAGES), make_results()),
        )
        self.assertTrue(first.success)
        self.assertEqual(first.data["replies_sent"], 1)

        # Same request still listed on the next scan - TikTok has not cleared
        # it from the queue yet, exactly like the real log.
        second_context = StubContext(observation_for(TikTokScreen.MESSAGES), make_results())
        second = await skill.execute(scan_task(), second_context)

        self.assertTrue(second.success)
        self.assertEqual(second.data["conversations_handled"][0]["status"], "already_handled")
        self.assertNotIn("platform.accept_message_request", second_context.actions)
        self.assertEqual(second_context.actions, [
            "platform.detect_message_requests",
            "platform.list_unread_conversations",
            "platform.open_message_requests",
            "platform.list_message_requests",
            "platform.open_message_request",
            "platform.return_to_primary_inbox",
        ])

    async def test_accepted_request_is_still_answered_when_content_disappears(self):
        # Regression guard: TikTok may drop the request bubble immediately
        # after Accept, leaving the post-accept thread empty. The real reply
        # must still be generated from the pre-accept content, via
        # `fallback_view` - never left unanswered just because the live view
        # went blank for a moment.
        provider = StubReplyProvider(ReplyDecision(
            text="Dạ shop đã nhận được yêu cầu ạ", should_send=True, model="stub"
        ))
        reply_skill = StubReplySkill(ActionResult(success=True, data={
            "decision": "complete_task", "send_status": "SENT",
        }))
        empty_after_accept = observation_for(
            TikTokScreen.CONVERSATION,
            messages=[], published=0, active="new_customer",
        )
        before_accept = observation_for(
            TikTokScreen.CONVERSATION,
            messages=[{
                "id": "request-before-accept", "text": "Shop tư vấn giúp mình",
                "direction": "INBOUND",
            }],
            active="new_customer",
        )
        context = StubContext(
            observation_for(TikTokScreen.MESSAGES),
            {
                "platform.detect_message_requests": ActionResult(
                    success=True, data={"tool": {"present": True, "count": 1}}
                ),
                "platform.list_unread_conversations": ActionResult(
                    success=True, data={"tool": {"conversations": []}}
                ),
                "platform.open_message_requests": ActionResult(
                    success=True, data={"tool": {"success": True}}
                ),
                "platform.list_message_requests": ActionResult(
                    success=True,
                    data={"tool": {"conversations": ["new_customer"]}},
                ),
                "platform.open_message_request": (
                    ActionResult(success=True, data={"tool": {"success": True}}),
                    before_accept,
                ),
                "platform.accept_message_request": (
                    ActionResult(success=True, data={"tool": {"success": True}}),
                    empty_after_accept,
                ),
                "platform.return_to_primary_inbox": (
                    ActionResult(success=True, data={"tool": {"success": True}}),
                    observation_for(TikTokScreen.MESSAGES),
                ),
            },
        )
        skill = InboxScanSkill(
            reply_provider=provider,
            reply_skill=reply_skill,
            auto_accept_message_requests=True,
            ledger=StubLedger(),
        )

        result = await skill.execute(scan_task(), context)

        outcome = result.data["conversations_handled"][0]
        self.assertEqual(outcome["status"], "sent")
        self.assertEqual(result.data["replies_sent"], 1)
        # The LLM saw the pre-accept content even though the live post-accept
        # view was empty.
        self.assertEqual(len(provider.contexts), 1)
        self.assertEqual(provider.contexts[0].message_text, "Shop tư vấn giúp mình")
        self.assertEqual(len(reply_skill.tasks), 2)
        self.assertEqual(
            reply_skill.tasks[0].payload["response"],
            "Dạ shop đã nhận được tin nhắn, mình kiểm tra và phản hồi ngay ạ.",
        )
        self.assertEqual(
            reply_skill.tasks[1].payload["response"], "Dạ shop đã nhận được yêu cầu ạ",
        )

    async def test_pre_send_recheck_retries_then_falls_back_to_the_accept_snapshot(self):
        """Regression: some TikTok request-to-chat transitions never render
        the pre-accept message into the live thread at all - only the
        "Message request accepted" banner shows, indefinitely (observed in
        production: candidates=1 notices=1 unchanged across 5+ independent
        re-opens over two minutes). The pre-send recheck must retry a couple
        of times, then fall back to the same fallback_view the reply was
        generated from instead of escalating - see PRE_SEND_RECHECK_RETRIES
        in platforms/tiktok/inbox_watcher.py."""
        provider = StubReplyProvider(ReplyDecision(
            text="Dạ shop đã nhận được yêu cầu ạ", should_send=True, model="stub"
        ))
        reply_skill = StubReplySkill(ActionResult(success=True, data={
            "decision": "complete_task", "send_status": "SENT",
        }))
        empty_after_accept = observation_for(
            TikTokScreen.CONVERSATION, messages=[], published=0, active="new_customer",
        )
        before_accept = observation_for(
            TikTokScreen.CONVERSATION,
            messages=[{
                "id": "request-before-accept", "text": "Shop tư vấn giúp mình",
                "direction": "INBOUND",
            }],
            active="new_customer",
        )
        context = StubContext(
            observation_for(TikTokScreen.MESSAGES),
            {
                "platform.detect_message_requests": ActionResult(
                    success=True, data={"tool": {"present": True, "count": 1}}
                ),
                "platform.list_unread_conversations": ActionResult(
                    success=True, data={"tool": {"conversations": []}}
                ),
                "platform.open_message_requests": ActionResult(
                    success=True, data={"tool": {"success": True}}
                ),
                "platform.list_message_requests": ActionResult(
                    success=True,
                    data={"tool": {"conversations": ["new_customer"]}},
                ),
                "platform.open_message_request": (
                    ActionResult(success=True, data={"tool": {"success": True}}),
                    before_accept,
                ),
                "platform.accept_message_request": (
                    ActionResult(success=True, data={"tool": {"success": True}}),
                    empty_after_accept,
                ),
                "platform.return_to_primary_inbox": (
                    ActionResult(success=True, data={"tool": {"success": True}}),
                    observation_for(TikTokScreen.MESSAGES),
                ),
            },
        )
        read_calls = []

        async def always_untrusted():
            read_calls.append(1)
            return SimpleNamespace(thread_trusted=False, thread_source="none")

        context.platform = SimpleNamespace(
            state_verifier=SimpleNamespace(read_conversation=always_untrusted)
        )
        skill = InboxScanSkill(
            reply_provider=provider,
            reply_skill=reply_skill,
            auto_accept_message_requests=True,
            ledger=StubLedger(),
        )

        with unittest.mock.patch(
            "platforms.tiktok.inbox_watcher.PRE_SEND_RECHECK_RETRY_DELAY_SECONDS", 0,
        ):
            result = await skill.execute(scan_task(), context)

        outcome = result.data["conversations_handled"][0]
        self.assertEqual(outcome["status"], "sent")
        self.assertEqual(result.data["replies_sent"], 1)
        # `read_conversation` is also called once earlier, recovering from
        # the empty post-accept observation - what matters here is that the
        # pre-send recheck itself retried (1 initial + PRE_SEND_RECHECK_
        # RETRIES) rather than escalating on the very first empty read.
        from platforms.tiktok.inbox_watcher import PRE_SEND_RECHECK_RETRIES
        self.assertGreaterEqual(len(read_calls), 1 + PRE_SEND_RECHECK_RETRIES)
        self.assertEqual(
            reply_skill.tasks[-1].payload["response"], "Dạ shop đã nhận được yêu cầu ạ",
        )

    async def test_pre_send_recheck_retries_then_escalates_without_a_fallback(self):
        """The same retry-then-give-up path, but for an ordinary (non-accept)
        conversation with no fallback_view available: after the same
        retries, it must still escalate exactly as before - the fallback
        path must never fire when there is nothing safe to fall back to."""
        provider = StubReplyProvider(ReplyDecision(
            text="Dạ còn hàng ạ", should_send=True, model="stub",
        ))
        reply_skill = StubReplySkill(ActionResult(success=True, data={
            "decision": "complete_task", "send_status": "SENT",
        }))
        context = StubContext(
            observation_for(
                TikTokScreen.CONVERSATION,
                messages=[{
                    "id": "m1", "text": "Còn hàng không?", "direction": "INBOUND",
                }],
                active="customer",
            ),
            {
                "platform.list_unread_conversations": ActionResult(
                    success=True, data={"tool": {"conversations": []}}
                ),
            },
        )
        read_calls = []

        async def always_untrusted():
            read_calls.append(1)
            return SimpleNamespace(thread_trusted=False, thread_source="none")

        context.platform = SimpleNamespace(
            state_verifier=SimpleNamespace(read_conversation=always_untrusted)
        )
        skill = InboxScanSkill(
            reply_provider=provider, reply_skill=reply_skill,
            reply_quiet_period_seconds=0,
        )

        with unittest.mock.patch(
            "platforms.tiktok.inbox_watcher.PRE_SEND_RECHECK_RETRY_DELAY_SECONDS", 0,
        ):
            result = await skill.execute(scan_task(), context)

        outcome = result.data["active_conversation"]
        self.assertEqual(outcome["status"], "requires_human")
        self.assertEqual(outcome["reason"], "pre_send_recheck_unavailable")
        self.assertEqual(result.data["replies_sent"], 0)
        self.assertEqual(reply_skill.tasks, [])
        from platforms.tiktok.inbox_watcher import PRE_SEND_RECHECK_RETRIES
        self.assertEqual(len(read_calls), 1 + PRE_SEND_RECHECK_RETRIES)

    async def test_suspicious_message_request_is_persisted_but_never_accepted(self):
        request_thread = observation_for(
            TikTokScreen.CONVERSATION,
            messages=[{
                "id": "spam-1",
                "text": "Đầu tư crypto lợi nhuận cao https://bit.ly/example",
                "direction": "INBOUND",
            }],
            active="unknown_sender",
        )
        ledger = StubLedger()
        context = StubContext(
            observation_for(TikTokScreen.MESSAGES),
            {
                "platform.detect_message_requests": ActionResult(
                    success=True, data={"tool": {"present": True, "count": 1}}
                ),
                "platform.list_unread_conversations": ActionResult(
                    success=True, data={"tool": {"conversations": []}}
                ),
                "platform.open_message_requests": ActionResult(success=True),
                "platform.list_message_requests": ActionResult(
                    success=True, data={"tool": {"conversations": ["unknown_sender"]}}
                ),
                "platform.open_message_request": (
                    ActionResult(success=True), request_thread,
                ),
                "platform.return_to_primary_inbox": (
                    ActionResult(success=True), observation_for(TikTokScreen.MESSAGES),
                ),
            },
        )
        skill = InboxScanSkill(
            auto_accept_message_requests=True, ledger=ledger,
        )

        result = await skill.execute(scan_task(), context)

        outcome = result.data["conversations_handled"][0]
        self.assertEqual(outcome["status"], "requires_human")
        self.assertNotIn("platform.accept_message_request", context.actions)
        snapshot = next(call for call in ledger.calls if call[0] == "save_pre_accept_snapshot")
        self.assertEqual(snapshot[2]["classification"], "spam")

    async def test_request_preview_that_has_not_rendered_yet_is_re_read_not_escalated(self):
        """Reproduces a real production escalation: opening a Message request
        left the observation on MESSAGES carrying no thread at all - the
        preview has no composer, so the extension cannot claim CONVERSATION,
        and it routinely publishes before the bubbles render. Classifying
        from that single read withheld a genuine customer as
        `pre_accept_content_unreadable` and sent them to a human instead of
        accepting them. The trusted reader must get a chance first."""
        provider = StubReplyProvider(ReplyDecision(
            text="Dạ shop tư vấn ngay ạ", should_send=True, model="stub",
        ))
        reply_skill = StubReplySkill(ActionResult(success=True, data={
            "decision": "complete_task", "send_status": "SENT",
        }))
        message = {
            "id": "request-1", "text": "Shop còn hàng không ạ", "direction": "INBOUND",
        }
        # Exactly what the live log showed after the click: MESSAGES, no thread.
        unrendered = observation_for(TikTokScreen.MESSAGES)
        context = StubContext(
            observation_for(TikTokScreen.MESSAGES),
            {
                "platform.detect_message_requests": ActionResult(
                    success=True, data={"tool": {"present": True, "count": 1}}
                ),
                "platform.list_unread_conversations": ActionResult(
                    success=True, data={"tool": {"conversations": []}}
                ),
                "platform.open_message_requests": ActionResult(success=True),
                "platform.list_message_requests": ActionResult(
                    success=True, data={"tool": {"conversations": ["new_customer"]}}
                ),
                "platform.open_message_request": (
                    ActionResult(success=True), unrendered,
                ),
                "platform.accept_message_request": (
                    ActionResult(success=True),
                    observation_for(
                        TikTokScreen.CONVERSATION,
                        messages=[message], active="new_customer",
                    ),
                ),
                "platform.return_to_primary_inbox": (
                    ActionResult(success=True), observation_for(TikTokScreen.MESSAGES),
                ),
            },
        )

        async def reads_the_preview():
            return trusted_thread_state([message])

        context.platform = SimpleNamespace(
            state_verifier=SimpleNamespace(read_conversation=reads_the_preview)
        )
        skill = InboxScanSkill(
            reply_provider=provider, reply_skill=reply_skill,
            auto_accept_message_requests=True, ledger=StubLedger(),
        )

        result = await skill.execute(scan_task(), context)

        self.assertIn("platform.accept_message_request", context.actions)
        outcome = result.data["conversations_handled"][0]
        self.assertNotEqual(outcome["status"], "requires_human")
        # The re-read content is what the customer is answered from.
        self.assertEqual(provider.contexts[0].message_text, "Shop còn hàng không ạ")

    async def test_request_preview_that_never_renders_is_still_withheld(self):
        """The fail-closed half of the same gate. Accept is one-way and
        risk=high, so a preview that stays unreadable through every re-read
        must still be withheld for a person - the retry only ever adds
        evidence, it never softens the verdict."""
        ledger = StubLedger()
        context = StubContext(
            observation_for(TikTokScreen.MESSAGES),
            {
                "platform.detect_message_requests": ActionResult(
                    success=True, data={"tool": {"present": True, "count": 1}}
                ),
                "platform.list_unread_conversations": ActionResult(
                    success=True, data={"tool": {"conversations": []}}
                ),
                "platform.open_message_requests": ActionResult(success=True),
                "platform.list_message_requests": ActionResult(
                    success=True, data={"tool": {"conversations": ["new_customer"]}}
                ),
                "platform.open_message_request": (
                    ActionResult(success=True), observation_for(TikTokScreen.MESSAGES),
                ),
                "platform.return_to_primary_inbox": (
                    ActionResult(success=True), observation_for(TikTokScreen.MESSAGES),
                ),
            },
        )
        read_calls = []

        async def never_renders():
            read_calls.append(1)
            return SimpleNamespace(thread_trusted=False, thread_source="none")

        context.platform = SimpleNamespace(
            state_verifier=SimpleNamespace(read_conversation=never_renders)
        )
        skill = InboxScanSkill(
            auto_accept_message_requests=True, ledger=ledger,
        )

        with unittest.mock.patch(
            "platforms.tiktok.inbox_watcher.PRE_SEND_RECHECK_RETRY_DELAY_SECONDS", 0,
        ):
            result = await skill.execute(scan_task(), context)

        outcome = result.data["conversations_handled"][0]
        self.assertEqual(outcome["status"], "requires_human")
        self.assertEqual(outcome["reason"], "pre_accept_content_unreadable")
        self.assertNotIn("platform.accept_message_request", context.actions)
        from platforms.tiktok.inbox_watcher import PRE_SEND_RECHECK_RETRIES
        self.assertEqual(len(read_calls), 1 + PRE_SEND_RECHECK_RETRIES)

    async def test_new_cancel_message_discards_old_image_decision_before_send(self):
        initial = [{
            "id": "m1", "text": "Gửi ảnh Milo cho mình", "direction": "INBOUND",
        }]
        cancelled = initial + [{
            "id": "m2", "text": "Không cần gửi ảnh nữa", "direction": "INBOUND",
        }]

        class ChangingProvider:
            def __init__(self):
                self.contexts = []

            async def reply(self, conversation):
                self.contexts.append(conversation)
                if len(self.contexts) == 1:
                    return ReplyDecision(
                        text="Dạ em gửi ảnh ạ", should_send=True,
                        attachments=(Attachment(
                            kind="image", path="/fixture/milo.jpg", name="milo.jpg"
                        ),),
                    )
                return ReplyDecision(
                    text="Dạ shop đã ghi nhận, em không gửi ảnh nữa ạ",
                    should_send=True,
                )

        provider = ChangingProvider()
        reply_skill = StubReplySkill(ActionResult(
            success=True, data={"send_status": "SENT"}
        ))
        context = StubContext(
            observation_for(
                TikTokScreen.CONVERSATION, messages=initial, active="customer"
            ),
            {
                "platform.list_unread_conversations": ActionResult(
                    success=True, data={"tool": {"conversations": []}}
                ),
            },
        )

        async def read_cancelled():
            return trusted_thread_state(cancelled)

        context.platform = SimpleNamespace(
            state_verifier=SimpleNamespace(read_conversation=read_cancelled)
        )
        skill = InboxScanSkill(
            reply_provider=provider, reply_skill=reply_skill,
            reply_quiet_period_seconds=0,
        )

        result = await skill.execute(scan_task(), context)

        self.assertEqual(result.data["active_conversation"]["status"], "sent")
        self.assertEqual(len(provider.contexts), 2)
        self.assertEqual(len(reply_skill.tasks), 1)
        self.assertEqual(reply_skill.tasks[0].payload["attachments"], [])
        self.assertEqual(reply_skill.tasks[0].payload["inbound_message_id"], "m2")

    async def test_conversation_without_reply_provider_is_still_a_successful_scan(self):
        result = await InboxScanSkill().execute(scan_task(), self._context())
        self.assertTrue(result.success)
        self.assertEqual(
            result.data["conversations_handled"][0]["status"], "handed_to_pipeline"
        )

    async def test_reply_routed_to_human_does_not_fail_the_scan(self):
        provider = StubReplyProvider(ReplyDecision.human("routed_to_human:complaint"))
        skill = InboxScanSkill(
            reply_provider=provider, reply_skill=StubReplySkill(None)
        )
        result = await skill.execute(scan_task(), self._context())
        self.assertTrue(result.success)
        self.assertEqual(
            result.data["conversations_handled"][0]["status"], "requires_human"
        )
        self.assertEqual(result.data["replies_sent"], 0)

    async def test_unverified_send_is_reported_without_failing_the_scan(self):
        provider = StubReplyProvider(ReplyDecision(
            text="Dạ còn hàng ạ", should_send=True, model="stub"
        ))
        reply_skill = StubReplySkill(
            ActionResult(success=False, error="Message remained unverified")
        )
        skill = InboxScanSkill(reply_provider=provider, reply_skill=reply_skill)
        result = await skill.execute(scan_task(), self._context())
        self.assertTrue(result.success)
        self.assertEqual(
            result.data["conversations_handled"][0]["status"],
            "reconcile_required",
        )

    async def test_media_only_customer_message_reaches_reply_provider(self):
        provider = StubReplyProvider(ReplyDecision(
            text="Mình đã nhận được ảnh. Bạn cần shop hỗ trợ gì ạ?",
            should_send=True, reason="generated", model="stub",
        ))
        skill = InboxScanSkill(
            reply_provider=provider,
            reply_skill=StubReplySkill(ActionResult(
                success=True, data={"decision": "complete_task", "send_status": "SENT"},
            )),
        )
        opened = observation_for(
            TikTokScreen.CONVERSATION,
            messages=[{
                "id": "media-only-1", "text": "", "direction": "INBOUND",
                "media": [{"kind": "image", "src": "blob:customer-image"}],
            }],
            active="customer",
        )
        context = StubContext(opened, {
            "platform.list_unread_conversations": ActionResult(
                success=True, data={"tool": {"conversations": []}},
            ),
        })

        result = await skill.execute(scan_task(), context)

        self.assertEqual(result.data["active_conversation"]["status"], "sent")
        self.assertEqual(len(provider.contexts), 1)
        self.assertEqual(provider.contexts[0].message_text, "")
        self.assertEqual(provider.contexts[0].inbound_media, [{
            "kind": "image", "src": "blob:customer-image",
        }])


class UnansweredRevisitTests(unittest.IsolatedAsyncioTestCase):
    """Opening a thread clears its unread marker, so a customer read but not
    answered would vanish from the unread list forever. The scan brings them
    back by name instead of waiting for them to write again."""

    def _context(self, unread_rows):
        opened = observation_for(
            TikTokScreen.CONVERSATION,
            messages=[{"id": "m1", "text": "Còn hàng không?", "direction": "INBOUND"}],
            active="customer",
        )
        opens = {
            name: (ActionResult(success=True, data={"tool": {"success": True}}), opened)
            for name in ("platform.open_unread_conversation", "platform.open_conversation")
        }
        return StubContext(
            observation_for(TikTokScreen.MESSAGES),
            {
                "platform.list_unread_conversations": ActionResult(
                    success=True, data={"tool": {"conversations": list(unread_rows)}}
                ),
                **opens,
            },
        )

    async def test_stable_id_and_username_alias_are_reopened_only_once(self):
        stable_id = "0:1:6840000000000000000:6980000000000000000"
        ledger = StubLedger(unanswered={
            stable_id: {"name": "customer", "attempts": 2},
            "customer": {"name": "customer", "attempts": 1},
        })
        skill = InboxScanSkill(
            reply_provider=StubReplyProvider(ReplyDecision.human("unused")),
            reply_skill=StubReplySkill(ActionResult(success=True, data={})),
            ledger=ledger,
        )

        def opened(_action):
            return (
                ActionResult(success=True, data={"tool": {"success": True}}),
                observation_for(
                    TikTokScreen.CONVERSATION,
                    messages=[{"id": "x1", "text": "?", "direction": "UNKNOWN"}],
                    active="customer",
                    rows=[{"username": "customer", "key": stable_id}],
                ),
            )

        context = StubContext(observation_for(TikTokScreen.MESSAGES), {
            "platform.list_unread_conversations": ActionResult(
                success=True, data={"tool": {"conversations": []}}
            ),
            "platform.open_conversation": opened,
        })

        await skill.execute(scan_task(), context)

        self.assertEqual(context.actions.count("platform.open_conversation"), 1)
        self.assertEqual(set(skill._unanswered), {stable_id})
        self.assertEqual(skill._unanswered[stable_id]["attempts"], 3)
        self.assertIn(("forget", "customer", {}), ledger.calls)

    async def test_a_read_but_unanswered_customer_is_reopened_next_scan(self):
        # The customer is still writing and the hold gives up immediately, so
        # the first scan reads them without answering.
        skill = InboxScanSkill(
            reply_provider=StubReplyProvider(ReplyDecision(
                text="Dạ còn hàng ạ", should_send=True, model="stub"
            )),
            reply_skill=StubReplySkill(ActionResult(
                success=True, data={"decision": "complete_task", "send_status": "SENT"}
            )),
            reply_quiet_period_seconds=20,
            reply_wait_timeout_seconds=0,
        )
        first = self._context(["customer"])
        result = await skill.execute(scan_task(), first)
        self.assertIn("platform.open_unread_conversation", first.actions)
        self.assertEqual(
            result.data["conversations_handled"][0]["status"], "waiting_for_customer"
        )

        # TikTok now reports no unread rows at all - the marker was cleared by
        # that very read - yet the customer is still owed an answer.
        skill.reply_quiet_period_seconds = 0
        second = self._context([])

        result = await skill.execute(scan_task(), second)

        self.assertIn("platform.open_conversation", second.actions)
        self.assertEqual(result.data["replies_sent"], 1)

    async def test_failed_open_is_kept_for_the_next_scan(self):
        skill = InboxScanSkill(
            reply_provider=StubReplyProvider(ReplyDecision(
                text="Dạ shop đây ạ", should_send=True, model="stub"
            )),
            reply_skill=StubReplySkill(ActionResult(
                success=True, data={"decision": "complete_task", "send_status": "SENT"}
            )),
        )
        first = StubContext(
            observation_for(TikTokScreen.MESSAGES),
            {
                "platform.list_unread_conversations": ActionResult(
                    success=True, data={"tool": {"conversations": ["customer"]}}
                ),
                "platform.open_unread_conversation": ActionResult(
                    success=False, error="post-click verification timeout"
                ),
            },
        )

        result = await skill.execute(scan_task(), first)

        self.assertEqual(
            result.data["conversations_handled"][0]["status"], "open_unverified"
        )
        self.assertIn("customer", skill._unanswered)

        second = self._context([])
        result = await skill.execute(scan_task(), second)

        self.assertIn("platform.open_conversation", second.actions)
        self.assertEqual(result.data["replies_sent"], 1)

    async def test_stale_ledger_cannot_displace_the_only_fresh_unread_customer(self):
        skill = InboxScanSkill(
            max_conversations_per_scan=3,
            reply_provider=StubReplyProvider(ReplyDecision(
                text="Dạ shop đây ạ", should_send=True, model="stub"
            )),
            reply_skill=StubReplySkill(ActionResult(
                success=True, data={"decision": "complete_task", "send_status": "SENT"}
            )),
        )
        skill._unanswered = {
            name: {"name": name, "attempts": 2}
            for name in ("old-1", "old-2", "old-3")
        }

        def opened(action):
            target = action.args["target"]
            return (
                ActionResult(success=True, data={"tool": {"success": True}}),
                observation_for(
                    TikTokScreen.CONVERSATION,
                    messages=[{
                        "id": f"{target}-m1", "text": "Shop ơi",
                        "direction": "INBOUND",
                    }],
                    active=target,
                ),
            )

        context = StubContext(
            observation_for(TikTokScreen.MESSAGES),
            {
                "platform.list_unread_conversations": ActionResult(
                    success=True,
                    data={"tool": {"conversations": ["helloworld!!"]}},
                ),
                "platform.open_conversation": opened,
                "platform.open_unread_conversation": opened,
            },
        )

        result = await skill.execute(scan_task(), context)

        handled = [
            item["conversation"]
            for item in result.data["conversations_handled"]
        ]
        self.assertEqual(handled, ["old-1", "old-2", "helloworld!!"])
        self.assertEqual(result.data["replies_sent"], 3)
        self.assertIn("old-3", skill._unanswered)
        self.assertNotIn("helloworld!!", skill._unanswered)

    async def test_unanswered_ledger_overrides_negative_badge_outside_inbox(self):
        skill = InboxScanSkill(trust_negative_unread_badge=True)
        skill._unanswered = {
            "customer": {"name": "customer", "attempts": 1}
        }
        context = StubContext(
            observation_for(TikTokScreen.FEED),
            {
                "platform.detect_unread": ActionResult(success=True, data={"tool": {
                    "source": "extension", "unread": False,
                }}),
                "platform.open_inbox": (
                    ActionResult(success=True, data={"tool": {"success": True}}),
                    observation_for(TikTokScreen.MESSAGES),
                ),
                "platform.list_unread_conversations": ActionResult(
                    success=True, data={"tool": {"conversations": []}}
                ),
                "platform.open_conversation": ActionResult(
                    success=False, error="stop after proving revisit"
                ),
            },
        )

        await skill.execute(scan_task(), context)

        self.assertEqual(context.actions[:3], [
            "platform.detect_unread",
            "platform.open_inbox",
            "platform.list_unread_conversations",
        ])
        self.assertIn("platform.open_conversation", context.actions)

    async def test_an_unreadable_thread_survives_past_the_ordinary_retry_cap(self):
        """A thread whose messages cannot be extracted (`no_inbound_message`) is
        a technical read failure, not a security/content concern - see
        `TECHNICAL_RETRY_STATUSES` in platforms/tiktok/inbox_watcher.py. It gets
        `MAX_TECHNICAL_RETRIES` (30) instead of the ordinary `MAX_UNANSWERED_
        REVISITS` (3) before `_hand_over` is used, so a customer is not
        abandoned just because a DOM misread (fixed or not) repeats a few
        times - six attempts here stays well under that higher cap."""
        empty = observation_for(TikTokScreen.CONVERSATION, messages=[], active="customer")
        skill = InboxScanSkill()
        skill._unanswered = {"customer": {"name": "customer", "attempts": 0}}
        attempts_seen = []
        for _ in range(6):
            context = StubContext(
                observation_for(TikTokScreen.MESSAGES),
                {
                    "platform.list_unread_conversations": ActionResult(
                        success=True, data={"tool": {"conversations": []}}
                    ),
                    "platform.open_conversation": (
                        ActionResult(success=True, data={"tool": {"success": True}}),
                        empty,
                    ),
                    "platform.open_unread_conversation": (
                        ActionResult(success=True, data={"tool": {"success": True}}),
                        empty,
                    ),
                },
            )
            await skill.execute(scan_task(), context)
            attempts_seen.append(len(skill._unanswered))

        # Never dropped: every scan still finds the customer queued.
        self.assertEqual(attempts_seen, [1, 1, 1, 1, 1, 1])
        self.assertEqual(skill._unanswered["customer"]["attempts"], 6)

    async def test_an_answered_customer_is_not_reopened(self):
        skill = InboxScanSkill(
            reply_provider=StubReplyProvider(ReplyDecision(
                text="Dạ còn hàng ạ", should_send=True, model="stub"
            )),
            reply_skill=StubReplySkill(ActionResult(
                success=True, data={"decision": "complete_task", "send_status": "SENT"}
            )),
        )
        await skill.execute(scan_task(), self._context(["customer"]))

        second = self._context([])
        result = await skill.execute(scan_task(), second)

        self.assertNotIn("platform.open_conversation", second.actions)
        self.assertEqual(result.data["conversations_observed"], 0)


class ActiveConversationScanTests(unittest.IsolatedAsyncioTestCase):
    """The open thread is DOM truth; its row badge is not.

    TikTok marks the conversation it is currently showing as read, so a scan
    that only trusted `unread_rows` would never answer a customer who writes
    while their chat is on screen.
    """

    INBOUND = {"id": "m1", "text": "Còn hàng không?", "direction": "INBOUND"}

    def _skill(self, reply_result=None):
        provider = StubReplyProvider(ReplyDecision(
            text="Dạ còn hàng ạ", should_send=True, reason="generated", model="stub"
        ))
        reply_skill = StubReplySkill(reply_result or ActionResult(
            success=True, data={"decision": "complete_task", "send_status": "SENT"}
        ))
        return InboxScanSkill(
            reply_provider=provider, reply_skill=reply_skill
        ), provider, reply_skill

    @staticmethod
    def _context(messages, rows=(), active="customer"):
        return StubContext(
            observation_for(
                TikTokScreen.CONVERSATION, messages=messages, active=active
            ),
            {
                "platform.list_unread_conversations": ActionResult(
                    success=True, data={"tool": {"conversations": list(rows)}}
                ),
                "platform.open_unread_conversation": ActionResult(
                    success=True, data={"tool": {"success": True}}
                ),
            },
        )

    async def test_new_inbound_in_open_thread_is_answered_although_no_row_is_unread(self):
        skill, provider, reply_skill = self._skill()
        context = self._context([self.INBOUND])

        result = await skill.execute(scan_task(), context)

        self.assertTrue(result.success)
        self.assertEqual(result.data["replies_sent"], 1)
        self.assertEqual(result.data["active_conversation"]["status"], "sent")
        self.assertEqual(provider.contexts[0].message_text, "Còn hàng không?")
        self.assertEqual(reply_skill.tasks[0].payload["recipient"], "customer")
        self.assertEqual(reply_skill.tasks[0].payload["inbound_message_id"], "m1")
        # The thread was already open: it is never opened or re-opened again.
        self.assertEqual(context.actions, ["platform.list_unread_conversations"])

    async def test_inbound_already_answered_is_marked_handled_without_sending(self):
        skill, provider, reply_skill = self._skill()
        context = self._context([
            self.INBOUND,
            {"id": "m2", "text": "Dạ còn hàng ạ", "direction": "OUTBOUND"},
        ])

        result = await skill.execute(scan_task(), context)

        self.assertEqual(result.data["active_conversation"]["status"], "already_replied")
        self.assertEqual(result.data["replies_sent"], 0)
        self.assertEqual(provider.contexts, [])
        self.assertEqual(reply_skill.tasks, [])

    async def test_same_inbound_on_a_later_scan_is_not_handled_twice(self):
        skill, provider, reply_skill = self._skill()
        await skill.execute(scan_task(), self._context([self.INBOUND]))

        # The reply has not rendered yet, so the thread still shows only the
        # inbound message. The per-conversation ledger is what prevents a
        # second send here.
        result = await skill.execute(scan_task(), self._context([self.INBOUND]))

        self.assertEqual(result.data["active_conversation"]["status"], "already_handled")
        self.assertEqual(len(provider.contexts), 1)
        self.assertEqual(len(reply_skill.tasks), 1)

    async def test_a_newer_inbound_message_is_handled_again(self):
        skill, provider, _ = self._skill()
        await skill.execute(scan_task(), self._context([self.INBOUND]))

        result = await skill.execute(scan_task(), self._context([
            self.INBOUND,
            {"id": "m2", "text": "Dạ còn hàng ạ", "direction": "OUTBOUND"},
            {"id": "m3", "text": "Ship về Hà Nội bao lâu?", "direction": "INBOUND"},
        ]))

        self.assertEqual(result.data["active_conversation"]["status"], "sent")
        self.assertEqual(len(provider.contexts), 2)
        self.assertEqual(provider.contexts[1].message_text, "Ship về Hà Nội bao lâu?")

    async def test_unverified_send_requires_reconciliation_and_is_not_retried(self):
        skill, provider, _ = self._skill(
            ActionResult(success=False, error="Message remained unverified")
        )
        first = await skill.execute(scan_task(), self._context([self.INBOUND]))
        second = await skill.execute(scan_task(), self._context([self.INBOUND]))

        self.assertEqual(
            first.data["active_conversation"]["status"], "reconcile_required"
        )
        self.assertEqual(
            second.data["active_conversation"]["status"], "already_handled"
        )
        self.assertEqual(len(provider.contexts), 1)

    async def test_active_thread_is_not_opened_again_when_its_row_is_unread(self):
        skill, _, _ = self._skill()
        context = self._context([self.INBOUND], rows=["customer"])

        result = await skill.execute(scan_task(), context)

        self.assertEqual(result.data["unread_detected"], 1)
        self.assertEqual(result.data["conversations_observed"], 0)
        self.assertEqual(result.data["replies_sent"], 1)
        self.assertNotIn("platform.open_unread_conversation", context.actions)

    async def test_other_unread_rows_are_scanned_after_the_active_thread(self):
        skill, provider, _ = self._skill()
        opened = observation_for(
            TikTokScreen.CONVERSATION,
            messages=[{"id": "n1", "text": "Shop ơi", "direction": "INBOUND"}],
            active="other",
        )
        context = StubContext(
            observation_for(
                TikTokScreen.CONVERSATION, messages=[self.INBOUND], active="customer"
            ),
            {
                "platform.list_unread_conversations": ActionResult(
                    success=True, data={"tool": {"conversations": ["customer", "other"]}}
                ),
                "platform.open_unread_conversation": (
                    ActionResult(success=True, data={"tool": {"success": True}}),
                    opened,
                ),
            },
        )

        result = await skill.execute(scan_task(), context)

        # The active thread first, then the one other unread row.
        self.assertEqual(result.data["conversations_observed"], 1)
        self.assertEqual(result.data["replies_sent"], 2)
        self.assertEqual(
            [item["conversation"] for item in result.data["conversations_handled"]],
            ["customer", "other"],
        )
        self.assertEqual(
            [entry.conversation_key for entry in provider.contexts],
            ["customer", "other"],
        )

    async def test_ledger_is_per_conversation(self):
        skill, provider, _ = self._skill()
        await skill.execute(scan_task(), self._context([self.INBOUND]))

        # Same message text in a different conversation must still be handled.
        result = await skill.execute(
            scan_task(), self._context([self.INBOUND], active="other")
        )

        self.assertEqual(result.data["active_conversation"]["status"], "sent")
        self.assertEqual(len(provider.contexts), 2)

    async def test_open_thread_without_a_readable_header_falls_back_to_the_rows(self):
        skill, _, _ = self._skill()
        context = self._context([self.INBOUND], active="")

        result = await skill.execute(scan_task(), context)

        self.assertTrue(result.success)
        self.assertIsNone(result.data["active_conversation"])
        self.assertEqual(result.data["reason"], "conversation_panel_no_unread")


class QuietPeriodTests(unittest.IsolatedAsyncioTestCase):
    """A customer writing several messages in a row gets one answer to all.

    The agent waits until the unanswered messages stop changing, so it never
    cuts in halfway through a question. The wait happens *on the open thread*
    within a single scan: this customer is finished before the next one is
    opened, rather than being deferred to a later scan.

    `browser.snapshot` is what re-reads the thread during the hold, so the stub
    advances the patched clock on every snapshot: one poll is one tick of time.
    """

    BURST = [
        {"id": "m1", "text": "Shop ơi", "direction": "INBOUND"},
        {"id": "m2", "text": "Tôi muốn tư vấn", "direction": "INBOUND"},
    ]

    def setUp(self):
        self.clock = 1000.0
        patcher = unittest.mock.patch(
            "platforms.tiktok.inbox_watcher.monotonic", lambda: self.clock
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # The hold polls on a real timer; the stub clock supplies the passage of
        # time instead, so the tests do not sleep.
        async def _no_sleep(_seconds):
            return None

        sleep_patcher = unittest.mock.patch(
            "platforms.tiktok.inbox_watcher.asyncio.sleep", _no_sleep
        )
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)
        self.provider = StubReplyProvider(ReplyDecision(
            text="Dạ shop nghe ạ", should_send=True, model="stub"
        ))
        self.reply_skill = StubReplySkill(ActionResult(
            success=True, data={"decision": "complete_task", "send_status": "SENT"}
        ))
        self.skill = InboxScanSkill(
            reply_provider=self.provider, reply_skill=self.reply_skill,
            reply_quiet_period_seconds=20,
            reply_wait_timeout_seconds=60,
            reply_wait_poll_seconds=0,
        )
        self.thread = list(self.BURST)
        # Messages the customer adds after N polls of the hold.
        self.arrivals = {}
        self.polls = 0

    async def _read_conversation(self):
        """One poll of the hold: time advances, the customer may have written.

        This is the extension-backed read the hold really uses; a Playwright
        `browser.snapshot` carries no rendered messages and must never be the
        source here.
        """
        self.polls += 1
        self.clock += 5
        self.thread = self.thread + self.arrivals.pop(self.polls, [])
        return trusted_thread_state(self.thread)

    def _context(self, messages):
        context = StubContext(
            observation_for(
                TikTokScreen.CONVERSATION, messages=messages, active="customer"
            ),
            {
                "platform.list_unread_conversations": ActionResult(
                    success=True, data={"tool": {"conversations": []}}
                ),
            },
        )
        context.platform = SimpleNamespace(
            state_verifier=SimpleNamespace(read_conversation=self._read_conversation)
        )
        return context

    async def _scan(self, messages=None):
        self.thread = list(messages if messages is not None else self.thread)
        result = await self.skill.execute(scan_task(), self._context(self.thread))
        return result.data["active_conversation"]

    async def test_the_burst_is_answered_within_the_scan_that_found_it(self):
        """The customer is not deferred to a later scan any more."""
        active = await self._scan(self.BURST)

        self.assertEqual(active["status"], "sent")
        self.assertEqual(len(self.provider.contexts), 1)
        # It held rather than replying immediately.
        self.assertGreaterEqual(self.polls, 4)

    async def test_a_new_message_restarts_the_waiting_period(self):
        # The customer writes again on the third poll, 15s into the wait.
        self.arrivals = {
            3: [{"id": "m3", "text": "về sữa ông thọ", "direction": "INBOUND"}]
        }

        active = await self._scan(self.BURST)

        self.assertEqual(active["status"], "sent")
        self.assertEqual(len(self.provider.contexts), 1)
        # The clock restarted on m3, so the hold ran past the first 20s window.
        self.assertGreaterEqual(self.polls, 7)
        # And the answer covers the whole burst, m3 included.
        self.assertEqual(
            [item["id"] for item in self.provider.contexts[0].pending_messages],
            ["m1", "m2", "m3"],
        )

    async def test_a_takeover_during_the_hold_releases_the_thread_at_once(self):
        """Before this, `agent_may_reply` was only consulted *after* the hold,
        so clicking Takeover while the agent was holding a conversation was
        noticed up to `reply_wait_timeout_seconds` (30s live) later."""
        control = TakeoverAfterPolls(self.skill, after_polls=2)
        self.skill.conversation_control = control

        active = await self._scan(self.BURST)

        self.assertEqual(active["status"], "human_controlled")
        # No reply was generated for a conversation a person now owns.
        self.assertEqual(self.provider.contexts, [])
        # It stopped when the takeover landed, not at the cap.
        self.assertLessEqual(control.checks, 6)

    async def test_a_queued_staff_reply_takes_the_chrome_session(self):
        """The agent runs one task at a time on one Chrome session. A staff
        reply is submitted at priority 100 so it is claimed the moment this
        task ends - which is no help while a hold is running, so the hold
        hands the session over instead of timing out."""
        context = self._context(self.BURST)
        queue = PriorityQueueAfterPolls(after_calls=2)
        context.memory = SimpleNamespace(repository=queue)

        result = await self.skill.execute(scan_task(), context)
        active = result.data["active_conversation"]

        self.assertEqual(active["status"], "yielded_to_priority_task")
        self.assertEqual(self.provider.contexts, [])
        # The customer is still owed an answer, and is queued again for it.
        self.assertIn("customer", self.skill._unanswered)

    async def test_yielding_never_spends_a_retry(self):
        """Three yields must not escalate a perfectly healthy conversation:
        the agent stopped for its own reasons, which says nothing about
        whether the thread can be answered. See NEUTRAL_RETRY_STATUSES."""
        from platforms.tiktok.scan_outcomes import MAX_UNANSWERED_REVISITS

        handoffs = []
        self.skill.handoff = SimpleNamespace(
            open=lambda **kwargs: handoffs.append(kwargs) or _resolved(),
        )
        for _ in range(MAX_UNANSWERED_REVISITS):
            context = self._context(self.BURST)
            context.memory = SimpleNamespace(
                repository=PriorityQueueAfterPolls(after_calls=1)
            )
            result = await self.skill.execute(scan_task(), context)
            await asyncio.sleep(0)

            self.assertEqual(
                result.data["active_conversation"]["status"],
                "yielded_to_priority_task",
            )
            # Counting these would read 1, 2, 3 here and escalate on the next
            # scan; the conversation is queued again either way.
            self.assertEqual(self.skill._unanswered["customer"]["attempts"], 0)

        self.assertEqual(handoffs, [])

    async def test_the_hold_keeps_going_when_the_queue_cannot_be_read(self):
        """Not knowing whether something is queued is no reason to drop a
        customer mid-burst."""
        class Broken:
            async def has_pending_above_priority(self, priority):
                raise RuntimeError("database is gone")

        context = self._context(self.BURST)
        context.memory = SimpleNamespace(repository=Broken())

        result = await self.skill.execute(scan_task(), context)

        self.assertEqual(result.data["active_conversation"]["status"], "sent")

    async def test_a_blind_read_does_not_empty_the_burst(self):
        """Regression: the hold used to read through `browser.snapshot`, which
        carries no rendered messages. An empty read looked like "nothing left to
        answer", so the third customer of a scan was read and then dropped."""
        reads = {"count": 0}

        async def blind_then_good():
            reads["count"] += 1
            self.clock += 5
            if reads["count"] <= 2:
                return trusted_thread_state(())   # đọc hụt
            return trusted_thread_state(self.thread)

        context = self._context(self.BURST)
        context.platform.state_verifier.read_conversation = blind_then_good

        result = await self.skill.execute(scan_task(), context)
        active = result.data["active_conversation"]

        self.assertEqual(active["status"], "sent")
        self.assertEqual(len(self.provider.contexts), 1)

    async def test_a_reader_that_never_works_is_not_allowed_to_send_stale_reply(self):
        """A broken final re-read must fail closed instead of sending stale media.

        The burst remains recorded by the ledger, while a person is asked to
        inspect the thread. Without a final watermark the agent cannot know
        whether the customer cancelled an attachment during generation.
        """
        reads = {"count": 0}

        async def always_blind():
            reads["count"] += 1
            self.clock += 5
            return trusted_thread_state(())

        context = self._context(self.BURST)
        context.platform.state_verifier.read_conversation = always_blind

        result = await self.skill.execute(scan_task(), context)
        active = result.data["active_conversation"]

        self.assertEqual(active["status"], "requires_human")
        self.assertEqual(active["reason"], "pre_send_recheck_unavailable")
        self.assertEqual(len(self.provider.contexts), 1)
        self.assertEqual(
            [item["id"] for item in self.provider.contexts[0].pending_messages],
            ["m1", "m2"],
        )
        self.assertGreaterEqual(reads["count"], 4)

    async def test_a_customer_who_never_stops_is_left_to_the_next_scan(self):
        """Better a late answer than one cutting into a sentence."""
        self.arrivals = {
            poll: [{"id": f"n{poll}", "text": "...", "direction": "INBOUND"}]
            for poll in range(1, 40)
        }

        active = await self._scan(self.BURST)

        self.assertEqual(active["status"], "waiting_for_customer")
        self.assertEqual(self.provider.contexts, [])

    async def test_the_whole_burst_is_answered_by_one_reply(self):
        await self._scan(self.BURST)

        context = self.provider.contexts[0]
        self.assertEqual(context.message_text, "Shop ơi\nTôi muốn tư vấn")
        self.assertEqual([item["id"] for item in context.pending_messages], ["m1", "m2"])
        # The signature keys on the last message of the burst.
        self.assertEqual(context.message_id, "m2")
        self.assertEqual(len(self.reply_skill.tasks), 1)
        self.assertEqual(self.reply_skill.tasks[0].payload["inbound_message_id"], "m2")

    async def test_messages_already_answered_are_not_part_of_the_burst(self):
        await self._scan([
            {"id": "m0", "text": "Chào shop", "direction": "INBOUND"},
            {"id": "m1", "text": "Dạ shop chào bạn", "direction": "OUTBOUND"},
        ] + self.BURST)

        self.assertEqual(
            [item["id"] for item in self.provider.contexts[0].pending_messages],
            ["m1", "m2"],
        )



async def _resolved(value=None):
    return value


class TakeoverAfterPolls:
    """`agent_may_reply` turns False once the hold has polled a few times."""

    def __init__(self, skill, *, after_polls):
        self.skill = skill
        self.after_polls = after_polls
        self.checks = 0

    async def agent_may_reply(self, conversation_id):
        self.checks += 1
        return self.checks <= self.after_polls


class PriorityQueueAfterPolls:
    """The task repository, reporting a staff reply queued after N asks."""

    def __init__(self, *, after_calls):
        self.after_calls = after_calls
        self.calls = 0

    async def has_pending_above_priority(self, priority):
        self.calls += 1
        return self.calls > self.after_calls


class ConversationIdentityTests(unittest.IsolatedAsyncioTestCase):
    """Everything persisted is keyed on TikTok's own thread id.

    A username can be changed by the customer; the thread key cannot.
    """

    THREAD = "0:1:6981093337449300994:7278178670136558593"

    def _skill(self):
        provider = StubReplyProvider(ReplyDecision(
            text="Dạ shop nghe ạ", should_send=True, model="stub"
        ))
        reply_skill = StubReplySkill(ActionResult(
            success=True, data={"decision": "complete_task", "send_status": "SENT"}
        ))
        return InboxScanSkill(
            reply_provider=provider, reply_skill=reply_skill
        ), provider, reply_skill

    def _context(self, rows):
        return StubContext(
            observation_for(
                TikTokScreen.CONVERSATION,
                messages=[{"id": "m1", "text": "Shop ơi", "direction": "INBOUND"}],
                active="customer",
                rows=rows,
            ),
            {
                "platform.list_unread_conversations": ActionResult(
                    success=True, data={"tool": {"conversations": []}}
                ),
            },
        )

    async def test_thread_key_of_the_open_row_is_used_as_conversation_id(self):
        skill, provider, reply_skill = self._skill()
        result = await skill.execute(scan_task(), self._context([
            {"username": "someone-else", "key": "0:1:1:2"},
            {"username": "customer", "key": self.THREAD},
        ]))

        self.assertEqual(
            result.data["active_conversation"]["conversation_id"], self.THREAD
        )
        self.assertEqual(provider.contexts[0].conversation_key, self.THREAD)
        # The username still identifies who to answer and which header to match.
        self.assertEqual(provider.contexts[0].customer_name, "customer")
        self.assertEqual(reply_skill.tasks[0].payload["conversation_id"], self.THREAD)
        self.assertEqual(reply_skill.tasks[0].payload["recipient"], "customer")

    async def test_conversation_id_falls_back_to_the_username(self):
        skill, provider, _ = self._skill()
        result = await skill.execute(scan_task(), self._context([]))

        self.assertEqual(
            result.data["active_conversation"]["conversation_id"], "customer"
        )
        self.assertEqual(provider.contexts[0].conversation_key, "customer")


class SentReplyRecordingTests(unittest.IsolatedAsyncioTestCase):
    """Only a reply the send verification confirmed is written to history."""

    def _run(self, reply_result):
        provider = StubReplyProvider(ReplyDecision(
            text="Dạ còn hàng ạ", should_send=True, model="stub"
        ))
        skill = InboxScanSkill(
            reply_provider=provider, reply_skill=StubReplySkill(reply_result)
        )
        context = StubContext(
            observation_for(
                TikTokScreen.CONVERSATION,
                messages=[{"id": "m1", "text": "Còn hàng không?", "direction": "INBOUND"}],
                active="customer",
                rows=[{"username": "customer", "key": "0:1:9:8"}],
            ),
            {
                "platform.list_unread_conversations": ActionResult(
                    success=True, data={"tool": {"conversations": []}}
                ),
            },
        )
        return skill, provider, context

    async def test_a_verified_send_is_recorded_against_the_thread_key(self):
        skill, provider, context = self._run(ActionResult(
            success=True, data={"decision": "complete_task", "send_status": "SENT"}
        ))

        await skill.execute(scan_task(), context)

        self.assertEqual(provider.recorded, [("0:1:9:8", "Dạ còn hàng ạ", ())])

    async def test_an_unverified_send_is_not_recorded(self):
        skill, provider, context = self._run(
            ActionResult(success=False, error="Message remained unverified")
        )

        await skill.execute(scan_task(), context)

        self.assertEqual(provider.recorded, [])
        self.assertEqual(provider.recorded_unverified, [])

    async def test_an_unverified_send_visible_on_screen_is_recorded_as_unverified(self):
        """Send was pressed and the reply is the newest bubble, but the thread
        could not place it after the inbound. It is on TikTok, so it must
        reach the dashboard - marked unverified - while the conversation
        still goes to a person and the reply is never retried."""
        skill, provider, context = self._run(ActionResult(
            success=False,
            error="send was not confirmed",
            data={"send_status": "UNCERTAIN", "reply_visible": True},
        ))

        result = await skill.execute(scan_task(), context)

        self.assertEqual(
            result.data["active_conversation"]["status"], "reconcile_required"
        )
        self.assertEqual(provider.recorded, [])
        self.assertEqual(
            provider.recorded_unverified, [("0:1:9:8", "Dạ còn hàng ạ", ())]
        )

    async def test_an_unverified_send_not_on_screen_is_not_recorded(self):
        skill, provider, context = self._run(ActionResult(
            success=False,
            error="send was not confirmed",
            data={"send_status": "UNCERTAIN", "reply_visible": False},
        ))

        await skill.execute(scan_task(), context)

        self.assertEqual(provider.recorded, [])
        self.assertEqual(provider.recorded_unverified, [])


if __name__ == "__main__":
    unittest.main()


class RecordingEscalation:
    """The `message_escalations` writer, without a database."""

    def __init__(self):
        self.rows = []

    async def escalate(self, event, state, reason, analysis=None):
        self.rows.append((event.conversation_id, event.message_id, reason))


class RecordingBus:
    def __init__(self):
        self.events = []

    def publish(self, event_type, **payload):
        self.events.append((event_type, payload))


class HumanHandoffTests(unittest.IsolatedAsyncioTestCase):
    """A conversation the agent will not answer has to reach a person.

    Before this, the scan only logged: no escalation row, no dashboard
    notification. The customer was dropped in silence.
    """

    THREAD = [{"id": "m1", "text": "Cho mình hoàn tiền", "direction": "INBOUND"}]

    def setUp(self):
        from services.handoff import HumanHandoff

        self.escalation = RecordingEscalation()
        self.bus = RecordingBus()
        self.handoff = HumanHandoff(
            account_id="acc_test",
            escalation_service=self.escalation,
            events=self.bus,
        )

    def _context(self, messages):
        context = StubContext(
            observation_for(
                TikTokScreen.CONVERSATION, messages=messages, active="customer"
            ),
            {
                "platform.list_unread_conversations": ActionResult(
                    success=True, data={"tool": {"conversations": []}}
                ),
            },
        )
        context.platform = SimpleNamespace(state_verifier=SimpleNamespace())
        return context

    async def test_a_withheld_reply_opens_an_escalation(self):
        skill = InboxScanSkill(
            reply_provider=StubReplyProvider(
                ReplyDecision.human("routed_to_human:return_request")
            ),
            reply_skill=StubReplySkill(ActionResult(success=True, data={})),
            handoff=self.handoff,
        )

        result = await skill.execute(scan_task(), self._context(self.THREAD))

        self.assertEqual(
            result.data["active_conversation"]["status"], "requires_human"
        )
        self.assertEqual(len(self.escalation.rows), 1)
        conversation, message_id, reason = self.escalation.rows[0]
        self.assertEqual(message_id, "m1")
        self.assertIn("return_request", reason)
        self.assertEqual(
            [item[0] for item in self.bus.events], ["escalation.opened"]
        )
        self.assertEqual(self.bus.events[0][1]["reason"], reason)

    async def test_ack_send_is_attempted_before_the_handoff_flips_conversation_control(self):
        """Regression: `_hand_over` (via `conversation_control.wait_for_human`)
        must never run before the courtesy `HUMAN_HANDOFF_ACK` it is
        responsible for delivering - a policy gate keyed on conversation_
        control (core/policy.py) cannot tell that ack apart from any other
        agent-origin send, and would block it if the handoff already flipped
        the mode away from AUTO. The ack's send attempt must happen first;
        the escalation is recorded right after, regardless of whether the
        send itself was confirmed."""
        send_seen_escalation_count = []

        class RecordingReplySkill:
            def __init__(self, outer):
                self.outer = outer

            async def execute(self, task, context):
                send_seen_escalation_count.append(len(self.outer.escalation.rows))
                return ActionResult(success=True, data={
                    "decision": "complete_task", "send_status": "SENT",
                })

        skill = InboxScanSkill(
            reply_provider=StubReplyProvider(ReplyDecision(
                text="Dạ, shop đã nhận được yêu cầu của bạn ạ.",
                should_send=True, requires_human=True,
                reason="required_tool_unavailable", model="human-handoff-ack",
            )),
            reply_skill=RecordingReplySkill(self),
            handoff=self.handoff,
        )

        result = await skill.execute(scan_task(), self._context(self.THREAD))

        # No escalation row existed yet at the moment the ack was sent.
        self.assertEqual(send_seen_escalation_count, [0])
        # Exactly one escalation row exists afterward - the handoff still
        # happens, just after the ack instead of before it.
        self.assertEqual(len(self.escalation.rows), 1)
        _, message_id, reason = self.escalation.rows[0]
        self.assertEqual(message_id, "m1")
        self.assertIn("required_tool_unavailable", reason)
        self.assertEqual(result.data["active_conversation"]["status"], "sent")

    async def test_a_sent_reply_opens_nothing(self):
        skill = InboxScanSkill(
            reply_provider=StubReplyProvider(
                ReplyDecision(text="Dạ shop nghe ạ", should_send=True, model="stub")
            ),
            reply_skill=StubReplySkill(ActionResult(
                success=True, data={"decision": "complete_task", "send_status": "SENT"}
            )),
            handoff=self.handoff,
        )

        result = await skill.execute(scan_task(), self._context(self.THREAD))

        self.assertEqual(result.data["active_conversation"]["status"], "sent")
        self.assertEqual(self.escalation.rows, [])
        self.assertEqual(self.bus.events, [])

    async def test_a_technically_unreadable_thread_does_not_reach_a_person_early(self):
        """`no_inbound_message` is `TECHNICAL_RETRY_STATUSES` (a DOM read
        failure, never a security/content concern) - it gets `MAX_TECHNICAL_
        RETRIES` (30) instead of the ordinary `MAX_UNANSWERED_REVISITS` (3)
        before an escalation opens, so a slow render or two never pages a
        person. See `TECHNICAL_RETRY_STATUSES` in
        platforms/tiktok/inbox_watcher.py."""
        skill = InboxScanSkill(
            reply_provider=StubReplyProvider(ReplyDecision.human("unused")),
            reply_skill=StubReplySkill(ActionResult(success=True, data={})),
            handoff=self.handoff,
        )
        # A thread whose messages carry no direction is exactly the case the
        # ledger keeps retrying.
        unreadable = [{"id": "x1", "text": "?", "direction": "UNKNOWN"}]

        for _ in range(8):
            await skill.execute(scan_task(), self._context(unreadable))
        # A scheduled-but-never-fired handoff would still show up here.
        await asyncio.sleep(0)

        reasons = [row[2] for row in self.escalation.rows]
        self.assertFalse(
            any("unreadable_after" in reason for reason in reasons),
            f"a purely technical read failure was escalated to a human: {reasons}",
        )
        self.assertNotIn("escalation.opened", [item[0] for item in self.bus.events])
        self.assertIn("customer", skill._unanswered)

    async def test_a_technically_unreadable_thread_eventually_reaches_a_person(self):
        """Past `MAX_TECHNICAL_RETRIES`, a technical read failure is no
        longer "a slow render" - it is broken in a way retrying does not fix,
        and must finally page a person instead of occupying a scan slot
        forever with nothing to show for it."""
        skill = InboxScanSkill(
            reply_provider=StubReplyProvider(ReplyDecision.human("unused")),
            reply_skill=StubReplySkill(ActionResult(success=True, data={})),
            handoff=self.handoff,
        )
        unreadable = [{"id": "x1", "text": "?", "direction": "UNKNOWN"}]

        from platforms.tiktok.scan_outcomes import MAX_TECHNICAL_RETRIES
        for _ in range(MAX_TECHNICAL_RETRIES + 1):
            await skill.execute(scan_task(), self._context(unreadable))
        await asyncio.sleep(0)

        reasons = [row[2] for row in self.escalation.rows]
        self.assertTrue(
            any("unreadable_after" in reason for reason in reasons),
            f"a persistently unreadable thread never reached a human: {reasons}",
        )
        self.assertIn("escalation.opened", [item[0] for item in self.bus.events])
        # Given up on: no longer retried every scan.
        self.assertNotIn("customer", skill._unanswered)

    async def test_an_answered_thread_is_not_treated_as_a_read_failure(self):
        """A thread whose newest rendered message is ours has nothing
        unanswered in it. Reading it is a success, so it must not accumulate
        `no_inbound_message` retries and must never page a person: that is
        what re-opened one already-answered thread on every scan and then
        escalated it as `unreadable_after_30_attempts`."""
        skill = InboxScanSkill(
            reply_provider=StubReplyProvider(ReplyDecision.human("unused")),
            reply_skill=StubReplySkill(ActionResult(success=True, data={})),
            handoff=self.handoff,
        )
        answered = [{"id": "o1", "text": "Dạ shop gửi thông tin ạ", "direction": "OUTBOUND"}]

        from platforms.tiktok.scan_outcomes import MAX_TECHNICAL_RETRIES
        for _ in range(MAX_TECHNICAL_RETRIES + 2):
            result = await skill.execute(scan_task(), self._context(answered))
        await asyncio.sleep(0)

        self.assertEqual(
            result.data["active_conversation"]["status"], "nothing_to_answer"
        )
        self.assertEqual(self.escalation.rows, [])
        self.assertNotIn("escalation.opened", [item[0] for item in self.bus.events])
        self.assertEqual(skill._unanswered, {})

    async def test_an_answered_thread_clears_a_retry_row_left_by_a_failed_read(self):
        """The first reads of a thread can genuinely fail (a slow render) and
        queue it for a revisit. Once a later read shows the thread answered,
        that queue entry has to go, or the conversation stays in the retry
        queue forever with its attempts frozen."""
        skill = InboxScanSkill(
            reply_provider=StubReplyProvider(ReplyDecision.human("unused")),
            reply_skill=StubReplySkill(ActionResult(success=True, data={})),
            handoff=self.handoff,
        )
        forgotten = []
        skill.ledger = SimpleNamespace(
            load=lambda: _done(({}, {})),
            forget=lambda key: _done(forgotten.append(key)),
            settle=lambda *a, **k: _done(None),
            mark_unanswered=lambda *a, **k: _done(None),
        )
        skill._ledger_loaded = True
        skill._unanswered = {"customer": {"name": "customer", "attempts": 4}}
        answered = [{"id": "o1", "text": "Dạ shop gửi thông tin ạ", "direction": "OUTBOUND"}]

        await skill.execute(scan_task(), self._context(answered))

        self.assertEqual(skill._unanswered, {})
        self.assertIn("customer", forgotten)


if __name__ == "__main__":
    unittest.main()


class StubLedger:
    """The durable ledger, without a database."""

    def __init__(self, settled=None, unanswered=None):
        self.stored = (settled or {}, unanswered or {})
        self.calls = []

    async def load(self):
        return self.stored

    async def settle(self, key, **fields):
        self.calls.append(("settle", key, fields))

    async def mark_unanswered(self, key, **fields):
        self.calls.append(("mark_unanswered", key, fields))

    async def forget(self, key):
        self.calls.append(("forget", key, {}))

    async def save_pre_accept_snapshot(self, key, **fields):
        self.calls.append(("save_pre_accept_snapshot", key, fields))

    async def mark_request_accepted(self, key):
        self.calls.append(("mark_request_accepted", key, {}))
        return True


async def _done(value):
    """A ready-made awaitable, for stubbing the ledger's async methods."""
    return value


class LedgerIsRestoredAndWrittenThrough(unittest.IsolatedAsyncioTestCase):
    """A restart must not lose the customers the agent has already read."""

    THREAD = [{"id": "m1", "text": "Còn hàng không shop", "direction": "INBOUND"}]

    def _context(self, messages, rows=()):
        context = StubContext(
            observation_for(
                TikTokScreen.CONVERSATION, messages=messages, active="customer"
            ),
            {
                "platform.list_unread_conversations": ActionResult(
                    success=True, data={"tool": {"conversations": list(rows)}}
                ),
                "platform.open_conversation": ActionResult(success=False, error="stop"),
            },
        )
        context.platform = SimpleNamespace(state_verifier=SimpleNamespace())
        return context

    def _skill(self, ledger, decision=None):
        return InboxScanSkill(
            reply_provider=StubReplyProvider(decision or ReplyDecision(
                text="Dạ còn ạ", should_send=True, model="stub"
            )),
            reply_skill=StubReplySkill(ActionResult(
                success=True, data={"decision": "complete_task", "send_status": "SENT"}
            )),
            ledger=ledger,
        )

    def test_repeated_text_with_old_fallback_id_is_not_mistaken_for_settled(self):
        skill = self._skill(StubLedger())
        earlier, repeated = ConversationView.from_items([
            {"id": "dom-collision", "text": "Mình muốn mua sữa",
             "direction": "INBOUND", "index": 10},
            {"id": "dom-collision", "text": "Mình muốn mua sữa",
             "direction": "INBOUND", "index": 22},
        ]).messages
        skill._settled["customer"] = {
            "inbound_message_id": earlier.id,
            "fingerprint": skill._fingerprint(earlier),
            "status": "sent",
        }

        self.assertTrue(skill._is_settled("customer", earlier))
        self.assertFalse(skill._is_settled("customer", repeated))

    def test_native_message_id_remains_authoritative_if_dom_position_moves(self):
        skill = self._skill(StubLedger())
        earlier, rerendered = ConversationView.from_items([
            {"id": "tiktok-native-123", "text": "Còn hàng không",
             "direction": "INBOUND", "index": 10},
            {"id": "tiktok-native-123", "text": "Còn hàng không",
             "direction": "INBOUND", "index": 22},
        ]).messages
        skill._settled["customer"] = {
            "inbound_message_id": earlier.id,
            "fingerprint": skill._fingerprint(earlier),
            "status": "sent",
        }

        self.assertTrue(skill._is_settled("customer", rerendered))

    async def test_sent_settle_uses_the_final_burst_fingerprint_not_initial_inbound(self):
        ledger = StubLedger()
        skill = self._skill(ledger)
        initial, final = ConversationView.from_items([
            {"id": "dom-initial", "text": "Địa chỉ của mình là",
             "direction": "INBOUND", "index": 10},
            {"id": "dom-final", "text": "Số 10 đường ABC",
             "direction": "INBOUND", "index": 11},
        ]).messages
        final_fingerprint = skill._fingerprint(final)

        await skill._settle("customer", {
            "conversation": "customer",
            "status": "sent",
            "inbound_message_id": final.id,
            "inbound_fingerprint": final_fingerprint,
        }, initial)

        self.assertEqual(
            skill._settled["customer"]["fingerprint"], final_fingerprint
        )
        self.assertTrue(skill._is_settled("customer", final))

    async def test_a_conversation_left_over_from_the_last_run_is_revisited(self):
        """Its unread marker is long gone; the stored row is the only way back."""
        ledger = StubLedger(
            unanswered={"leftover": {"name": "leftover", "attempts": 1}}
        )
        skill = self._skill(ledger)

        await skill.execute(scan_task(), self._context(self.THREAD))

        self.assertEqual(
            skill._unanswered.get("leftover"), {"name": "leftover", "attempts": 2}
        )

    async def test_an_answered_conversation_is_written_through(self):
        ledger = StubLedger()
        skill = self._skill(ledger)

        await skill.execute(scan_task(), self._context(self.THREAD))

        settled = [call for call in ledger.calls if call[0] == "settle"]
        self.assertEqual(len(settled), 1)
        self.assertEqual(settled[0][2]["inbound_message_id"], "m1")
        self.assertEqual(settled[0][2]["status"], "sent")

    async def test_an_unanswered_conversation_is_written_through(self):
        """A thread the agent could not read is queued again - durably.

        A conversation handed to a human is *settled*, not unanswered: a person
        owns it now and the agent must stop reopening it. The case that has to
        survive a restart is the one nobody owns yet.
        """
        ledger = StubLedger()
        skill = self._skill(ledger)
        unreadable = [{"id": "x1", "text": "?", "direction": "UNKNOWN"}]

        await skill.execute(scan_task(), self._context(unreadable))

        marked = [call for call in ledger.calls if call[0] == "mark_unanswered"]
        self.assertEqual(len(marked), 1)
        self.assertEqual(marked[0][2]["attempts"], 1)
        self.assertEqual(marked[0][2]["status"], "no_inbound_message")

    async def test_a_silent_handoff_stays_unanswered_until_an_ack_is_sent(self):
        ledger = StubLedger()
        skill = self._skill(ledger, ReplyDecision.human("routed_to_human:complaint"))

        await skill.execute(scan_task(), self._context(self.THREAD))

        calls = [call for call in ledger.calls if call[0] != "load"]
        self.assertEqual([call[0] for call in calls], ["mark_unanswered"])
        self.assertEqual(calls[0][2]["status"], "requires_human")

    async def test_a_ledger_that_cannot_be_read_does_not_stop_the_scan(self):
        class BrokenLedger(StubLedger):
            async def load(self):
                raise RuntimeError("postgres is down")

        skill = self._skill(BrokenLedger())

        result = await skill.execute(scan_task(), self._context(self.THREAD))

        self.assertTrue(result.success)
        self.assertEqual(result.data["active_conversation"]["status"], "sent")


class ActivityBadgeReloadTests(unittest.IsolatedAsyncioTestCase):
    """TikTok's message-request queue only refreshes on reload - see
    `InboxScanSkill._maybe_reload_for_new_activity`. It fires off the
    Activity badge, the only signal that updates live, and never blind."""

    def _context(self, screen, activity_unread=0, *, notifications_seen=0, reload_result=None):
        results = {
            "platform.list_unread_conversations": ActionResult(
                success=True, data={"tool": {"conversations": []}}
            ),
        }
        if reload_result is not None:
            results["platform.reload_page"] = reload_result
            # A reload lands on HOME, so the normal flow re-enters
            # `_detect_and_open`, which reads the badge before opening Messages.
            results["platform.detect_unread"] = ActionResult(
                success=True, data={"tool": {"unread": False}}
            )
            results["platform.open_inbox"] = ActionResult(success=True, data={})
        return StubContext(
            observation_for(
                screen, activity_unread=activity_unread, notifications_seen=notifications_seen,
            ),
            results,
        )

    async def test_first_observation_ever_only_records_a_baseline(self):
        skill = InboxScanSkill()
        context = self._context(TikTokScreen.MESSAGES, activity_unread=3)

        await skill.execute(scan_task(), context)

        self.assertNotIn("platform.reload_page", context.actions)
        self.assertEqual(skill._last_activity_count, 3)

    async def test_an_unchanged_badge_across_two_scans_never_reloads(self):
        skill = InboxScanSkill()
        await skill.execute(scan_task(), self._context(TikTokScreen.MESSAGES, activity_unread=2))

        context = self._context(TikTokScreen.MESSAGES, activity_unread=2)
        await skill.execute(scan_task(), context)

        self.assertNotIn("platform.reload_page", context.actions)

    async def test_a_rising_badge_from_the_messages_screen_triggers_one_reload(self):
        skill = InboxScanSkill()
        await skill.execute(scan_task(), self._context(TikTokScreen.MESSAGES, activity_unread=1))

        context = self._context(
            TikTokScreen.MESSAGES, activity_unread=2,
            reload_result=(
                ActionResult(success=True, data={}),
                observation_for(TikTokScreen.HOME, activity_unread=2),
            ),
        )
        await skill.execute(scan_task(), context)

        self.assertEqual(context.actions[0], "platform.reload_page")

    async def test_a_rising_badge_mid_conversation_never_reloads(self):
        """An unsent draft or an in-flight send must never be wiped out from
        under the operator/customer by a page reload."""
        skill = InboxScanSkill()
        await skill.execute(scan_task(), self._context(TikTokScreen.CONVERSATION, activity_unread=1))

        context = self._context(TikTokScreen.CONVERSATION, activity_unread=5)
        await skill.execute(scan_task(), context)

        self.assertNotIn("platform.reload_page", context.actions)

    async def test_a_falling_badge_never_reloads(self):
        skill = InboxScanSkill()
        await skill.execute(scan_task(), self._context(TikTokScreen.MESSAGES, activity_unread=4))

        context = self._context(TikTokScreen.MESSAGES, activity_unread=1)
        await skill.execute(scan_task(), context)

        self.assertNotIn("platform.reload_page", context.actions)

    async def test_a_real_page_notification_alone_triggers_one_reload(self):
        """The Activity badge can stay flat while a real Notification still
        fires - notifications_seen is its own independent trigger."""
        skill = InboxScanSkill()
        await skill.execute(scan_task(), self._context(TikTokScreen.MESSAGES, notifications_seen=0))

        context = self._context(
            TikTokScreen.MESSAGES, notifications_seen=1,
            reload_result=(
                ActionResult(success=True, data={}),
                observation_for(TikTokScreen.HOME, notifications_seen=1),
            ),
        )
        await skill.execute(scan_task(), context)

        self.assertEqual(context.actions[0], "platform.reload_page")

    async def test_an_unchanged_notification_count_never_reloads(self):
        skill = InboxScanSkill()
        await skill.execute(scan_task(), self._context(TikTokScreen.MESSAGES, notifications_seen=2))

        context = self._context(TikTokScreen.MESSAGES, notifications_seen=2)
        await skill.execute(scan_task(), context)

        self.assertNotIn("platform.reload_page", context.actions)

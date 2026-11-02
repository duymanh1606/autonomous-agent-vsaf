import asyncio
import tempfile
import unittest
import json
from pathlib import Path

from core.graph import AgentLoop
from core.memory import Memory
from core.policy import PolicyEngine
from core.state import AgentState
from models.action import Action
from models.auth import AuthState
from models.message_delivery import MessageSendState
from models.observation import Observation
from models.result import ActionResult
from models.tasks import Task
from perception.browser_observer import BrowserObserver
from perception.state_parser import ConversationRow, ParsedDomState
from perception.vision_fallback import VisionFallback, VisionResult
from platforms.tiktok.screens import TikTokScreen
from platforms.tiktok.messaging import BatchMessageSkill, ReplyMessageSkill
from platforms.tiktok_web.adapter import TikTokWebAdapter
from platforms.tiktok_web.selectors import SELECTORS
from tools.browser import ChromeBrowserTool
from tools.registry import ToolRegistry


class StubBrowser:
    pass


class StubObserver:
    async def observe(self):
        raise AssertionError("A mismatched platform must fail before browser observation")


class VirtualizedUnreadConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_badge_with_no_visible_unread_row_scans_virtualized_list(self):
        class Browser:
            def __init__(self): self.calls = []
            async def list_unread_conversations(self, limit, selectors):
                self.calls.append(limit)
                return {"success": True, "source": "virtual_scroll", "conversations": ["older-unread"]}

        class DomObserver:
            async def observe_state(self):
                return ParsedDomState(
                    screen=TikTokScreen.MESSAGES, confidence=0.97,
                    unread_badge=True, unread_count=7,
                    conversations=(ConversationRow(name="visible-read", unread=False),),
                )

        browser = Browser()
        adapter = TikTokWebAdapter(
            browser, StubObserver(), dom_observer=DomObserver()
        )

        result = await adapter.list_unread_conversations(3)

        self.assertEqual(result["conversations"], ["older-unread"])
        self.assertEqual(browser.calls, [3])


class MessageRequestAcceptSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_accept_is_clicked_and_report_is_never_clicked(self):
        class Node:
            def __init__(self, text="", aria=""):
                self.text = text
                self.aria = aria
                self.clicked = False
            async def is_visible(self): return True
            async def is_enabled(self): return True
            async def inner_text(self): return self.text
            async def get_attribute(self, name):
                return self.aria if name == "aria-label" else None
            async def click(self, **kwargs): self.clicked = True

        class Locator:
            def __init__(self, nodes=()): self.nodes = list(nodes)
            async def count(self): return len(self.nodes)
            def nth(self, index): return self.nodes[index]
            @property
            def first(self): return self.nodes[0]
            async def wait_for(self, **kwargs): return None

        header = Node("new_customer")
        report = Node("Report", "Report")
        accept = Node("Accept", "Accept")
        chatbox = Node()

        class Page:
            def get_by_role(self, role, name=None):
                # Playwright applies the exact anchored regex before returning
                # this collection, so Report is deliberately absent.
                return Locator([accept])
            def locator(self, selector):
                if selector in SELECTORS["chat_target"]:
                    return Locator([header])
                if selector == SELECTORS["chatbox"][0]:
                    return Locator([chatbox])
                if selector in SELECTORS["message_request_accept"]:
                    return Locator([report])
                return Locator()

        class Browser(ChromeBrowserTool):
            async def _ensure_started(self): return None
            async def current_extension_sequence(self): return 41

        browser = Browser.__new__(Browser)
        browser._page = Page()

        result = await browser.accept_message_request("new_customer", SELECTORS)

        self.assertTrue(result["success"])
        self.assertEqual(result["source"], "dom_exact_accessible_name")
        self.assertTrue(accept.clicked)
        self.assertFalse(report.clicked)


class MediaAttachmentBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_button_file_chooser_stages_a_verified_image(self):
        root = Path(tempfile.mkdtemp())
        image = root / "front.jpg"
        image.write_bytes(b"\xff\xd8\xfftest-image")

        class Input:
            def __init__(self): self.observations = 0
            async def evaluate(self, expression, args):
                self.observations += 1
                return {
                    "retained": True, "preview": True,
                    "preview_kind": "img", "busy": False, "error": "",
                }

        class Chooser:
            def __init__(self, element):
                self.element = element
                self.selected = None
            async def set_files(self, path): self.selected = path

        class ChooserInfo:
            def __init__(self, chooser): self.chooser = chooser
            async def __aenter__(self): return self
            async def __aexit__(self, exc_type, exc, traceback): return False
            @property
            def value(self):
                async def resolved(): return self.chooser
                return resolved()

        class Empty:
            async def count(self): return 0

        class Button:
            def __init__(self, on_click=None):
                self.clicked = False
                self.on_click = on_click
            async def count(self): return 1
            def nth(self, index): return self
            async def is_visible(self): return True
            async def is_disabled(self): return False
            async def click(self):
                self.clicked = True
                if self.on_click: self.on_click()

        input_element = Input()
        chooser = Chooser(input_element)
        button = Button()
        modal = {"visible": True}
        confirm = Button(lambda: modal.update(visible=False))

        class Dialog:
            async def count(self): return 1
            def nth(self, index): return self
            async def is_visible(self): return modal["visible"]

        dialog = Dialog()

        class Page:
            def locator(self, selector):
                if "input" in selector: return Empty()
                if ":has(" in selector: return dialog
                if "send-media-btn" in selector: return confirm
                return button
            def expect_file_chooser(self, timeout): return ChooserInfo(chooser)
            async def wait_for_timeout(self, milliseconds): return None

        browser = ChromeBrowserTool(
            allowed_media_root=root, media_stage_timeout_ms=1000
        )
        browser._probe_video_duration = lambda _path: 10.0
        browser._page = Page()

        async def ready(): return None
        async def sequence(): return 11
        browser._ensure_started = ready
        browser._current_extension_sequence = sequence
        async def fresh(*args, **kwargs): return {"sequence": 12}
        browser._wait_for_fresh_extension_state = fresh

        result = await browser.attach_message_media(
            image, ("input[type=file]",), ("img",), ("progress",),
            ("[role=alert]",),
            ('[data-e2e="dm-new-media-btn"][role="button"]',),
            ('[role="dialog"] [data-e2e="dm-new-send-media-btn"]',),
            ('[role="dialog"]:has([data-e2e="dm-new-send-media-btn"])',),
        )
        self.assertTrue(button.clicked)
        self.assertEqual(chooser.selected, str(image.resolve()))
        self.assertEqual(input_element.observations, 3)
        self.assertEqual(result["kind"], "image")
        self.assertEqual(result["source"], "file_chooser")
        self.assertEqual(result["previous_extension_sequence"], 11)
        self.assertTrue(confirm.clicked)
        self.assertEqual(result["confirmation"], {
            "clicked": True, "modal_closed": True, "fresh_state": True,
        })

    async def test_video_file_is_selected_and_reaches_stable_composer_state(self):
        root = Path(tempfile.mkdtemp())
        video = root / "intro.mp4"
        video.write_bytes(b"\x00\x00\x00\x18ftypmp42test-video")

        class Candidate:
            def __init__(self):
                self.selected = None
                self.observations = 0

            async def count(self): return 1
            def nth(self, index): return self
            async def is_disabled(self): return False
            async def get_attribute(self, name): return "video/mp4"
            async def set_input_files(self, path): self.selected = path
            async def evaluate(self, expression, args):
                self.observations += 1
                return {
                    "retained": True, "preview": True,
                    "preview_kind": "video", "busy": False, "error": "",
                }

        candidate = Candidate()

        class Page:
            def locator(self, selector): return candidate
            async def wait_for_timeout(self, milliseconds): return None

        browser = ChromeBrowserTool(
            allowed_media_root=root, media_stage_timeout_ms=1000
        )
        browser._probe_video_duration = lambda _path: 10.0
        browser._page = Page()

        async def ready(): return None
        async def sequence(): return 7
        browser._ensure_started = ready
        browser._current_extension_sequence = sequence

        result = await browser.attach_message_media(
            video, ("input[type=file]",), ("video",), ("progress",), ("[role=alert]",)
        )
        self.assertEqual(result["kind"], "video")
        self.assertEqual(result["previous_extension_sequence"], 7)
        self.assertEqual(candidate.selected, str(video.resolve()))
        self.assertEqual(candidate.observations, 6)

    async def test_video_over_configured_sixty_seconds_is_rejected_before_browser_access(self):
        root = Path(tempfile.mkdtemp())
        video = root / "long.mp4"
        video.write_bytes(b"\x00\x00\x00\x18ftypmp42test-video")
        browser = ChromeBrowserTool(
            allowed_media_root=root, max_dm_video_duration_seconds=60
        )
        browser._probe_video_duration = lambda _path: 60.01
        with self.assertRaisesRegex(ValueError, "duration"):
            await browser.attach_message_media(video, ("input[type=file]",))

    async def test_attachment_symlink_is_rejected_before_browser_access(self):
        root = Path(tempfile.mkdtemp())
        real = root / "real.jpg"
        real.write_bytes(b"\xff\xd8\xfftest")
        linked = root / "linked.jpg"
        linked.symlink_to(real)
        browser = ChromeBrowserTool(allowed_media_root=root)
        with self.assertRaisesRegex(ValueError, "symlink"):
            await browser.attach_message_media(linked, ("input[type=file]",))

    async def test_media_outside_product_root_is_rejected_before_browser_access(self):
        root = Path(tempfile.mkdtemp())
        outside = Path(tempfile.mkdtemp()) / "outside.mp4"
        outside.write_bytes(b"\x00\x00\x00\x18ftypmp42test-video")
        browser = ChromeBrowserTool(allowed_media_root=root)
        with self.assertRaisesRegex(ValueError, "outside"):
            await browser.attach_message_media(outside, ("input[type=file]",))

    async def test_disabled_media_send_surfaces_tiktok_validation_message(self):
        class Empty:
            async def count(self): return 0

        class DisabledButton:
            async def count(self): return 1
            def nth(self, index): return self
            async def is_visible(self): return True
            async def is_disabled(self): return True

        class Dialog:
            async def count(self): return 1
            def nth(self, index): return self
            async def is_visible(self): return True
            async def inner_text(self):
                return "Send media\nPlease unselect media that are longer than 15 seconds"

        class Page:
            def locator(self, selector):
                if selector == '[role="dialog"]': return Dialog()
                if "send-media-btn" in selector: return DisabledButton()
                return Empty()
            async def wait_for_timeout(self, ms): await asyncio.sleep(0)

        browser = ChromeBrowserTool(media_stage_timeout_ms=1000)
        browser._page = Page()
        with self.assertRaisesRegex(RuntimeError, "longer than 15 seconds"):
            await browser._confirm_message_media(
                ('[role="dialog"] [data-e2e="dm-new-send-media-btn"]',),
                ('[role="dialog"]',),
            )

    async def test_media_send_waits_for_a_dialog_that_mounts_after_staging(self):
        """An image is staged once the file input holds it, which can be before
        TikTok mounts its "Send media" dialog. One look used to fail the send
        with "confirmation button was not found"."""
        class Empty:
            async def count(self): return 0

        clicks = []

        class LateButton:
            looks = 0
            async def count(self):
                LateButton.looks += 1
                return 1 if LateButton.looks > 3 else 0
            def nth(self, index): return self
            async def is_visible(self): return True
            async def is_disabled(self): return False
            async def click(self): clicks.append(True)

        class Page:
            def locator(self, selector):
                if "send-media-btn" in selector and not clicks: return LateButton()
                return Empty()
            async def wait_for_timeout(self, ms): await asyncio.sleep(0)

        browser = ChromeBrowserTool(media_stage_timeout_ms=5000)
        browser._page = Page()
        async def sequence(): return 1
        async def fresh(*args, **kwargs): return {"sequence": 2}
        browser._current_extension_sequence = sequence
        browser._wait_for_fresh_extension_state = fresh

        result = await browser._confirm_message_media(
            ('[role="dialog"] [data-e2e="dm-new-send-media-btn"]',),
            ('[role="dialog"]:has([data-e2e="dm-new-send-media-btn"])',),
        )

        self.assertEqual(clicks, [True])
        self.assertTrue(result["modal_closed"])

    async def test_already_empty_draft_does_not_wait_for_a_dom_mutation(self):
        class Empty:
            async def count(self): return 0

        class Editor:
            @property
            def first(self): return self
            async def count(self): return 1
            async def is_visible(self): return True
            async def evaluate(self, expression): return ""

        editor = Editor()

        class Page:
            def locator(self, selector):
                return editor if selector == "editor" else Empty()
            async def wait_for_timeout(self, milliseconds): return None

        browser = ChromeBrowserTool()
        browser._page = Page()

        async def ready(): return None
        async def sequence(): return 66
        async def unexpected_wait(*args, **kwargs):
            raise AssertionError("an unchanged empty draft has no fresh DOM event")

        browser._ensure_started = ready
        browser._current_extension_sequence = sequence
        browser._wait_for_fresh_extension_state = unexpected_wait

        result = await browser.clear_message_draft(
            ("editor",), ("file",), ("draft-preview",),
            ("media-dialog",), ("cancel",), ("remove",),
        )

        self.assertTrue(result["draft_empty"])
        self.assertTrue(result["media_empty"])
        self.assertFalse(result["mutated"])
        self.assertEqual(result["previous_extension_sequence"], 66)


class SequenceObserver:
    def __init__(self, observations):
        self.observations = iter(observations)
        self.calls = 0

    async def observe(self):
        self.calls += 1
        return next(self.observations)


class StubScheduler:
    async def next_task(self):
        return None


class CountingScheduler:
    def __init__(self):
        self.calls = 0

    async def next_task(self):
        self.calls += 1
        return None


class StubRouter:
    async def route(self, task):
        return None


class StubPlanner:
    async def plan(self, task, observation, memory):
        raise AssertionError("Planner should not be called")


class StubRecovery:
    pass


class BrowserSafetyTests(unittest.TestCase):
    def test_navigation_rejects_non_tiktok_origin(self):
        browser = ChromeBrowserTool()
        with self.assertRaises(ValueError):
            browser._validate_url("https://example.com/")
        browser._validate_url("https://www.tiktok.com/messages")


class BrowserReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_navigation_context_errors_are_retried(self):
        class Page:
            def __init__(self):
                self.waits = []

            async def wait_for_timeout(self, milliseconds):
                self.waits.append(milliseconds)

        browser = ChromeBrowserTool(timeout_ms=2000)
        browser._page = Page()
        results = iter((
            RuntimeError("Execution context was destroyed, most likely because of a navigation"),
            RuntimeError("Cannot find context with specified id"),
            {"ready": True, "messages_ready": True, "visible_count": 4, "body_length": 20},
            {"ready": True, "messages_ready": True, "visible_count": 4, "body_length": 20},
        ))

        async def render_state():
            result = next(results)
            if isinstance(result, Exception):
                raise result
            return result

        browser._render_state = render_state
        state = await browser._wait_for_ui_ready()
        self.assertTrue(state["ready"])
        self.assertEqual(browser._page.waits, [250, 250, 250])

    async def test_non_navigation_render_error_is_not_hidden(self):
        browser = ChromeBrowserTool()

        async def render_state():
            raise RuntimeError("JavaScript syntax error")

        browser._render_state = render_state
        with self.assertRaisesRegex(RuntimeError, "syntax error"):
            await browser._wait_for_ui_ready()

    async def test_open_url_refuses_direct_navigation(self):
        """URL navigation is disabled by design: the extension owns the page."""
        navigated = []

        class Page:
            url = "https://www.tiktok.com/messages"

            async def goto(self, url, wait_until):
                navigated.append(url)

        browser = ChromeBrowserTool()
        browser._page = Page()
        with self.assertRaisesRegex(RuntimeError, "Direct URL navigation is disabled"):
            await browser.open_url("https://www.tiktok.com/messages")
        self.assertEqual(navigated, [])

    async def test_open_url_rejects_a_non_tiktok_host_before_anything_else(self):
        browser = ChromeBrowserTool()
        with self.assertRaises(ValueError):
            await browser.open_url("https://example.com/")


class BrowserObservationTests(unittest.TestCase):
    def snapshot(self, url, body="", elements=None):
        return {
            "url": url,
            "title": "TikTok",
            "body_text": body,
            "elements": elements or [],
            "session_cookie_names": [],
            "authenticated_dom": False,
            "login_form_visible": False,
        }

    def test_detects_main_web_screens_from_url(self):
        self.assertEqual(
            BrowserObserver._detect(self.snapshot("https://www.tiktok.com/foryou"))[0],
            TikTokScreen.VIDEO_FEED,
        )
        self.assertEqual(
            BrowserObserver._detect(self.snapshot("https://www.tiktok.com/messages"))[0],
            TikTokScreen.INBOX,
        )
        self.assertEqual(
            BrowserObserver._detect(self.snapshot("https://www.tiktok.com/@someone"))[0],
            TikTokScreen.PROFILE,
        )

    def test_message_input_distinguishes_chat_from_inbox(self):
        snapshot = self.snapshot(
            "https://www.tiktok.com/messages",
            elements=[{"resource_id": "message-input"}],
        )
        self.assertEqual(BrowserObserver._detect(snapshot)[0], TikTokScreen.CONVERSATION)

    def test_empty_chat_shell_does_not_turn_messages_into_conversation(self):
        snapshot = self.snapshot(
            "https://www.tiktok.com/messages",
            elements=[{"resource_id": "dm-new-chatbox"}],
        )
        self.assertEqual(BrowserObserver._detect(snapshot)[0], TikTokScreen.INBOX)

    def test_valid_protected_session_is_logged_in(self):
        snapshot = self.snapshot("https://www.tiktok.com/messages")
        snapshot["session_cookie_names"] = ["sessionid"]
        snapshot["authenticated_dom"] = True
        self.assertEqual(BrowserObserver._detect_auth(snapshot), AuthState.LOGGED_IN)

    def test_loading_protected_session_is_temporary_not_logged_out(self):
        snapshot = self.snapshot("https://www.tiktok.com/messages")
        snapshot["session_cookie_names"] = ["sessionid"]
        snapshot["page_loading"] = True
        self.assertEqual(
            BrowserObserver._detect_auth(snapshot), AuthState.TEMPORARY_ERROR
        )

    def test_login_form_requires_manual_login(self):
        snapshot = self.snapshot("https://www.tiktok.com/login")
        snapshot["login_form_visible"] = True
        self.assertEqual(BrowserObserver._detect_auth(snapshot), AuthState.AUTH_REQUIRED)

    def test_max_attempts_is_rate_limited_without_retry(self):
        snapshot = self.snapshot(
            "https://www.tiktok.com/login",
            body="Maximum number of attempts reached.",
        )
        self.assertEqual(
            BrowserObserver._detect_auth(snapshot),
            AuthState.AUTH_RATE_LIMITED,
        )

    def test_captcha_is_a_security_challenge(self):
        snapshot = self.snapshot(
            "https://www.tiktok.com/login",
            body="Complete the CAPTCHA to continue.",
        )
        self.assertEqual(
            BrowserObserver._detect_auth(snapshot),
            AuthState.SECURITY_CHALLENGE,
        )

    def test_screen_recognition_status_is_explicit(self):
        observer = BrowserObserver(StubBrowser(), VisionFallback(enabled=False))
        self.assertEqual(
            observer._recognition_status(TikTokScreen.MESSAGES, 0.99, False),
            "recognized",
        )
        self.assertEqual(
            observer._recognition_status(TikTokScreen.MESSAGES, 0.2, False),
            "low_confidence",
        )
        self.assertEqual(
            observer._recognition_status(TikTokScreen.UNKNOWN, 0.0, False),
            "unknown",
        )
        self.assertEqual(
            observer._recognition_status(TikTokScreen.SECURITY_CHALLENGE, 1.0, True),
            "human_required",
        )


class VisionRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_fenced_json_and_lowercase_states_are_accepted(self):
        class Analyzer:
            async def generate(self, *args, **kwargs):
                return """```json
                {"screen":"messages","confidence":0.95,"message_send_state":"uncertain","composer_state":"unknown","targets":[]}
                ```"""

        with tempfile.TemporaryDirectory() as directory:
            screenshot = Path(directory) / "screen.png"
            screenshot.write_bytes(b"not-decoded-by-test-analyzer")
            result = await VisionFallback(analyzer=Analyzer()).analyze(screenshot)
        self.assertEqual(result.screen, TikTokScreen.MESSAGES)
        self.assertEqual(result.confidence, 0.95)

    async def test_uninformative_image_is_captured_again_before_observation(self):
        class Browser:
            def __init__(self):
                self.captures = 0

            async def snapshot(self):
                return {
                    "url": "https://www.tiktok.com/unknown",
                    "title": "TikTok",
                    "body_text": "Rendered TikTok shell",
                    "elements": [],
                    "signature": "snapshot",
                    "session_cookie_names": ["sessionid"],
                    "authenticated_dom": True,
                    "login_form_visible": False,
                    "page_loading": False,
                }

            async def screenshot(self):
                self.captures += 1
                return f"capture-{self.captures}.png"

            async def set_visual_targets(self, targets, screenshot, threshold):
                return None

        class Vision:
            enabled = True

            def __init__(self):
                self.results = iter((
                    VisionResult(screen=TikTokScreen.UNKNOWN, confidence=0.2),
                    VisionResult(screen=TikTokScreen.FEED, confidence=0.95),
                ))

            async def analyze(self, screenshot, **kwargs):
                return next(self.results)

        browser = Browser()
        observation = await BrowserObserver(
            browser, Vision(), max_vision_capture_attempts=3,
            vision_retry_delay_seconds=0,
        ).observe()
        self.assertEqual(browser.captures, 2)
        self.assertEqual(observation.screen, TikTokScreen.FEED)
        self.assertEqual(observation.perception_source, "vision")


class ExtensionPerceptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_extension_state_requires_sequence_screen_and_event(self):
        browser = ChromeBrowserTool()
        states = iter([
            # Not newer than previous_sequence=5.
            {"sequence": 5, "ui_state": "MESSAGES", "events": ["CONVERSATION_LIST_CHANGED"]},
            # Newer and on the right screen, but not the awaited event.
            {"sequence": 6, "ui_state": "CONVERSATION", "events": ["CONVERSATION_LIST_CHANGED"]},
            # Newer, right screen, and STATE_CHANGED is a composer_changed alias.
            {"sequence": 7, "ui_state": "CONVERSATION", "events": ["STATE_CHANGED"]},
        ])
        last = {}

        async def read_state():
            nonlocal last
            last = next(states, last)
            return last

        browser._read_extension_state = read_state
        with self.assertLogs("tools.browser", level="INFO") as captured:
            state = await browser._wait_for_fresh_extension_state(
                5,
                expected_screen="conversation",
                expected_events={"composer_changed"},
            )
        self.assertEqual(state["sequence"], 7)
        self.assertTrue(browser._perception_wait_result)
        output = "\n".join(captured.output)
        self.assertIn("[EXT] Waiting for fresh state previous_sequence=5", output)
        self.assertIn("[EXT] Fresh state received seq=7 state=CONVERSATION", output)

    async def test_fresh_extension_timeout_marks_stale_state_for_dom_fallback(self):
        browser = ChromeBrowserTool()

        async def read_state():
            # Newer than previous_sequence=5, but never reaches CONVERSATION.
            return {
                "sequence": 6,
                "ui_state": "MESSAGES",
                "events": ["CONVERSATION_LIST_CHANGED"],
            }

        browser._read_extension_state = read_state
        with self.assertLogs("tools.browser", level="INFO") as captured:
            state = await browser._wait_for_fresh_extension_state(
                5,
                expected_screen="conversation",
                timeout=0.01,
            )
        self.assertIsNone(state)
        self.assertEqual(browser._extension_state_floor, 6)
        self.assertTrue(browser._force_dom_snapshot_once)
        self.assertFalse(browser._perception_wait_result)
        self.assertIn(
            "[EXT] Fresh state timeout - fallback to browser DOM",
            "\n".join(captured.output),
        )

    def test_extension_trace_logs_connection_and_screen_change_once(self):
        browser = ChromeBrowserTool()
        first = {
            "schema_version": 2,
            "platform": "tiktok_web",
            "sequence": 1,
            "url": "https://www.tiktok.com/messages",
            "ui_state": "MESSAGES",
            "page_confidence": 0.98,
            "conversations": [], "messages": [], "events": [],
            "modal": None, "composer": {}, "send_button": {},
            "messages_panel": {}, "active_chat": {}, "security_challenge": None,
        }
        second = {
            **first,
            "sequence": 2,
            "ui_state": "CONVERSATION",
            "page_confidence": 0.99,
        }
        with self.assertLogs("tools.browser", level="INFO") as captured:
            browser._trace_extension_payload(first)
            browser._trace_extension_payload(first)
            browser._trace_extension_payload(second)
        output = "\n".join(captured.output)
        self.assertEqual(output.count("[EXT] Connected"), 1)
        self.assertIn(
            "[EXT] state=MESSAGES mode=none seq=1 confidence=0.98", output
        )
        self.assertIn("[EXT] Screen changed=MESSAGES -> CONVERSATION", output)

    async def test_browser_accepts_valid_extension_bridge(self):
        class Locator:
            async def text_content(self, timeout):
                return json.dumps({
                    "schema_version": 2,
                    "platform": "tiktok_web",
                    "sequence": 4,
                    "url": "https://www.tiktok.com/messages",
                    "ui_state": "MESSAGES",
                    "page_confidence": 0.99,
                    "elements": [{
                        "id": "conversation_0",
                        "role": "listitem",
                        "text": "Customer", "visible": True,
                    }],
                })

        class Page:
            url = "https://www.tiktok.com/messages"

            def locator(self, selector):
                return Locator()

        browser = ChromeBrowserTool()
        browser._page = Page()
        state = await browser._read_extension_state()
        self.assertEqual(state["sequence"], 4)
        self.assertEqual(state["ui_state"], "MESSAGES")

    async def test_browser_rejects_incomplete_extension_bridge_for_snapshot_fallback(self):
        class Locator:
            async def text_content(self, timeout):
                return json.dumps({
                    "schema_version": 1,
                    "source": "autonomous-tiktok-perception-extension",
                    "sequence": 1,
                    "url": "https://www.tiktok.com/messages/123",
                    "screen": {"name": "conversation", "confidence": 0.99},
                    "elements": [],
                })

        class Page:
            url = "https://www.tiktok.com/messages/123"

            def locator(self, selector):
                return Locator()

        browser = ChromeBrowserTool()
        browser._page = Page()
        self.assertIsNone(await browser._read_extension_state())

    async def test_observer_waits_for_extension_event_after_action(self):
        class Browser:
            def __init__(self):
                self.waited = False

            async def wait_for_perception_event(
                self, timeout_ms, previous_sequence=None, expected_screen=None
            ):
                self.waited = True
                return True

            async def current_extension_sequence(self):
                return 1

            async def snapshot(self):
                return {
                    "url": "https://www.tiktok.com/messages",
                    "title": "TikTok",
                    "body_text": "Messages",
                    "elements": [{"resource_id": "dm-new-conversation-item", "visible": True}],
                    "signature": "extension-snapshot",
                    "session_cookie_names": ["sessionid"],
                    "authenticated_dom": True,
                    "login_form_visible": False,
                    "page_loading": False,
                    "extension_state": {
                        "sequence": 2,
                        "ui_state": "MESSAGES",
                        "page_confidence": 0.99,
                    },
                }

            async def set_visual_targets(self, *args):
                return None

        browser = Browser()
        observation = await BrowserObserver(
            browser, VisionFallback(enabled=False), extension_event_wait_ms=10
        ).observe(after_action="platform.open_inbox")
        self.assertTrue(browser.waited)
        self.assertEqual(observation.perception_source, "extension")
        self.assertEqual(observation.screen, TikTokScreen.MESSAGES)


class WebAdapterTests(unittest.TestCase):
    def test_adapter_refuses_business_action_until_logged_in(self):
        adapter = TikTokWebAdapter(StubBrowser(), StubObserver())
        action = adapter.next_action(
            Task(account_id="acc_test", id="w1", capability="open_inbox", platform="tiktok_web"),
            Observation(screen=TikTokScreen.UNKNOWN, unknown=True),
        )
        self.assertEqual(action.name, "abort_task")

    def test_message_input_requires_exact_composer_text(self):
        adapter = TikTokWebAdapter(StubBrowser(), StubObserver())
        action = Action(name="browser.input_text", args={"text": "Approved message"})
        verified = Observation(
            screen=TikTokScreen.CONVERSATION,
            confidence=1,
            auth_state=AuthState.LOGGED_IN,
            elements=[{"role": "textarea", "text": "Approved message"}],
        )
        missing = verified.model_copy(update={"elements": []})
        self.assertTrue(adapter.verify(action, None, verified))
        self.assertFalse(adapter.verify(action, None, missing))

    def test_sent_message_requires_dm_message_element(self):
        adapter = TikTokWebAdapter(StubBrowser(), StubObserver())
        action = Action(name="platform.send_message", args={"text": "Approved message"})
        sent = Observation(
            screen=TikTokScreen.CONVERSATION,
            confidence=1,
            auth_state=AuthState.LOGGED_IN,
            elements=[{
                "resource_id": "dm-new-message-text",
                "text": "Approved message",
            }],
        )
        draft_only = sent.model_copy(update={
            "elements": [{"role": "textbox", "text": "Approved message"}]
        })
        self.assertTrue(adapter.verify(action, None, sent))
        self.assertFalse(adapter.verify(action, None, draft_only))

    def test_search_requires_query(self):
        adapter = TikTokWebAdapter(StubBrowser(), StubObserver())
        action = adapter.next_action(
            Task(account_id="acc_test", id="w2", capability="search_content", platform="tiktok_web"),
            Observation(
                screen=TikTokScreen.VIDEO_FEED, confidence=1,
                auth_state=AuthState.LOGGED_IN,
            ),
        )
        self.assertEqual(action.name, "abort_task")


class MessagingSkillTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_message_requires_confirmation_before_ui_actions(self):
        skill = ReplyMessageSkill(input_action="browser.input_text")

        class NoUiContext:
            observation = Observation(
                screen=TikTokScreen.INBOX,
                auth_state=AuthState.LOGGED_IN,
            )

            async def execute_action(self, action):
                raise AssertionError("No UI action is allowed before confirm_send")

        task = Task(
            account_id="acc_test",
            id="message-1", capability="send_message", platform="tiktok_web",
            payload={"recipient": "Test User", "message": "Hello"},
        )
        self.assertTrue(await skill.can_handle(task, None))
        result = await skill.execute(task, NoUiContext())
        self.assertFalse(result.success)
        self.assertIn("confirm_send", result.error)

    async def test_proactive_message_is_never_generated_by_ai(self):
        skill = ReplyMessageSkill(input_action="browser.input_text")

        class NoUiContext:
            observation = Observation(
                screen=TikTokScreen.INBOX,
                auth_state=AuthState.LOGGED_IN,
            )

        task = Task(
            account_id="acc_test",
            id="message-2", capability="send_message", platform="tiktok_web",
            payload={"recipient": "Test User", "confirm_send": True},
        )
        result = await skill.execute(task, NoUiContext())
        self.assertFalse(result.success)
        self.assertIn("never AI-generated", result.error)

    async def test_send_message_executes_verified_action_sequence(self):
        skill = ReplyMessageSkill(input_action="browser.input_text")

        class RecordingContext:
            def __init__(self):
                self.observation = Observation(
                    screen=TikTokScreen.VIDEO_FEED,
                    auth_state=AuthState.LOGGED_IN,
                )
                self.actions = []

            async def execute_action(self, action):
                self.actions.append(action)
                if action.name == "platform.open_inbox":
                    self.observation = self.observation.model_copy(
                        update={"screen": TikTokScreen.INBOX}
                    )
                elif action.name == "platform.open_conversation":
                    self.observation = self.observation.model_copy(
                        update={
                            "screen": TikTokScreen.CONVERSATION,
                            "elements": [{
                                "resource_id": "dm-new-chat-nickname",
                                "text": "Test User",
                            }],
                        }
                    )
                elif action.name == "browser.input_text":
                    self.observation = self.observation.model_copy(update={
                        "elements": [
                            {
                                "resource_id": "dm-new-chat-nickname",
                                "text": "Test User",
                            },
                            {"role": "textarea", "text": action.args["text"]},
                        ]
                    })
                elif action.name == "platform.send_message":
                    self.observation = self.observation.model_copy(update={
                        "message_send_state": MessageSendState.SENT,
                        "metadata": {"extension_state": {"messages": [{
                            "id": "out-1", "text": action.args["text"],
                            "direction": "OUTBOUND", "index": 0,
                        }]}},
                    })
                return ActionResult(success=True)

        context = RecordingContext()
        task = Task(
            account_id="acc_test",
            id="message-3", capability="send_message", platform="tiktok_web",
            payload={
                "recipient": "Test User", "message": "Approved message",
                "confirm_send": True,
            },
        )
        result = await skill.execute(task, context)
        self.assertTrue(result.success)
        self.assertEqual(
            [action.name for action in context.actions],
            [
                "platform.open_inbox",
                "platform.open_conversation",
                "platform.focus_message_input",
                "browser.input_text",
                "platform.send_message",
            ],
        )
        self.assertEqual(context.actions[1].args["target"], "Test User")
        self.assertEqual(context.actions[-1].args["text"], "Approved message")


class BatchMessagingSkillTests(unittest.IsolatedAsyncioTestCase):
    class Repository:
        def __init__(self):
            self.tasks = []

        async def submit(self, task):
            self.tasks.append(task)

    class Context:
        def __init__(self, conversations):
            self.observation = Observation(
                screen=TikTokScreen.INBOX,
                auth_state=AuthState.LOGGED_IN,
            )
            self.repository = BatchMessagingSkillTests.Repository()
            self.memory = type("Memory", (), {"repository": self.repository})()
            self.conversations = conversations
            self.actions = []

        async def execute_action(self, action):
            self.actions.append(action)
            if action.name == "platform.list_conversations":
                return ActionResult(success=True, data={
                    "tool": {"conversations": self.conversations[:action.args["limit"]]}
                })
            return ActionResult(success=True)

    async def test_preview_never_enqueues_send_tasks(self):
        context = self.Context(["First", "Second"])
        result = await BatchMessageSkill().execute(Task(
            account_id="acc_test",
            id="batch-preview", capability="send_message_batch", platform="tiktok_web",
            payload={"count": 2, "preview_only": True},
        ), context)
        self.assertTrue(result.success)
        self.assertEqual(result.data["approved_recipients"], ["First", "Second"])
        self.assertEqual(context.repository.tasks, [])

    async def test_changed_inbox_order_aborts_batch(self):
        context = self.Context(["Second", "First"])
        result = await BatchMessageSkill().execute(Task(
            account_id="acc_test",
            id="batch-changed", capability="send_message_batch", platform="tiktok_web",
            payload={
                "count": 2,
                "approved_recipients": ["First", "Second"],
                "message": "Approved",
                "confirm_send": True,
            },
        ), context)
        self.assertFalse(result.success)
        self.assertEqual(context.repository.tasks, [])

    async def test_approved_batch_enqueues_individual_send_tasks(self):
        context = self.Context(["First", "Second"])
        result = await BatchMessageSkill().execute(Task(
            account_id="acc_test",
            id="batch-approved", capability="send_message_batch", platform="tiktok_web",
            payload={
                "count": 2,
                "approved_recipients": ["First", "Second"],
                "message": "Approved",
                "confirm_send": True,
            },
        ), context)
        self.assertTrue(result.success)
        self.assertEqual(len(context.repository.tasks), 2)
        self.assertEqual(
            [child.payload["recipient"] for child in context.repository.tasks],
            ["First", "Second"],
        )
        self.assertTrue(all(
            child.capability == "send_message" and child.payload["confirm_send"] is True
            for child in context.repository.tasks
        ))


class AuthPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_passive_auth_tools_are_allowed_before_login(self):
        policy = PolicyEngine(["browser.check_auth", "platform.open_inbox", "browser.input_text"])
        state = AgentState(auth_enforced=True, auth_state=AuthState.AUTH_REQUIRED)
        state.begin_task("auth")
        task = Task(account_id="acc_test", id="auth", capability="open_inbox", platform="tiktok_web")
        self.assertTrue(await policy.allow(
            Action(name="browser.check_auth"), state, task
        ))
        self.assertFalse(await policy.allow(
            Action(name="platform.open_inbox"), state, task
        ))

    async def test_browser_text_is_never_available_for_login_credentials(self):
        policy = PolicyEngine(["browser.input_text"])
        state = AgentState(auth_enforced=True, auth_state=AuthState.LOGGED_IN)
        state.begin_task("login")
        task = Task(
            account_id="acc_test",
            id="login", capability="login", platform="tiktok_web",
            payload={"confirm_send": True},
        )
        self.assertFalse(await policy.allow(
            Action(name="browser.input_text", args={"text": "secret"}), state, task
        ))

    async def test_future_login_tools_and_sensitive_args_are_denied(self):
        policy = PolicyEngine(["platform.login", "platform.open_search"])
        state = AgentState(auth_enforced=True, auth_state=AuthState.LOGGED_IN)
        state.begin_task("safe")
        task = Task(account_id="acc_test", id="safe", capability="search_content", platform="tiktok_web")
        self.assertFalse(await policy.allow(Action(
            name="platform.login", args={"username": "user", "password": "secret"}
        ), state, task))
        self.assertFalse(await policy.allow(Action(
            name="platform.open_search", args={"otp": "123456"}
        ), state, task))


class PlatformIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsupported_platform_task_cannot_run_on_web_adapter(self):
        adapter = TikTokWebAdapter(StubBrowser(), StubObserver())
        loop = AgentLoop(
            StubScheduler(), StubObserver(), StubRouter(), StubPlanner(),
            PolicyEngine([]), StubRecovery(), ToolRegistry(), adapter, Memory(), AgentState(),
        )
        result = await loop.run_task(Task(account_id="acc_test", id="wrong", capability="open_inbox", platform="tiktok"))
        self.assertFalse(result.success)
        self.assertIn("does not match", result.error)


class AuthGateLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_auth_does_not_claim_more_tasks_during_cooldown(self):
        scheduler = CountingScheduler()
        loop = AgentLoop(
            scheduler, StubObserver(), StubRouter(), StubPlanner(),
            PolicyEngine([]), StubRecovery(), ToolRegistry(),
            TikTokWebAdapter(StubBrowser(), StubObserver()), Memory(),
            AgentState(
                auth_enforced=True,
                auth_state=AuthState.AUTH_RATE_LIMITED,
                auth_blocked_until=float("inf"),
            ),
        )
        self.assertFalse(await loop.run_once())
        self.assertEqual(scheduler.calls, 0)

    async def test_auth_required_stops_before_router_without_retry(self):
        observation = Observation(
            screen=TikTokScreen.DIALOG,
            confidence=1,
            auth_state=AuthState.AUTH_REQUIRED,
        )
        observer = SequenceObserver([observation])
        adapter = TikTokWebAdapter(StubBrowser(), observer)
        loop = AgentLoop(
            StubScheduler(), observer, StubRouter(), StubPlanner(),
            PolicyEngine([]), StubRecovery(), ToolRegistry(), adapter, Memory(),
            AgentState(auth_enforced=True),
            auth_poll_seconds=0,
        )
        result = await loop.run_task(
            Task(account_id="acc_test", id="manual", capability="open_inbox", platform="tiktok_web")
        )
        self.assertFalse(result.success)
        self.assertIn("AUTH_REQUIRED", result.error)
        self.assertEqual(observer.calls, 1)

    async def test_rate_limited_stops_immediately_without_retry(self):
        observation = Observation(
            screen=TikTokScreen.DIALOG,
            confidence=1,
            security_challenge=True,
            auth_state=AuthState.AUTH_RATE_LIMITED,
        )
        observer = SequenceObserver([observation])
        adapter = TikTokWebAdapter(StubBrowser(), observer)
        loop = AgentLoop(
            StubScheduler(), observer, StubRouter(), StubPlanner(),
            PolicyEngine([]), StubRecovery(), ToolRegistry(), adapter, Memory(),
            AgentState(auth_enforced=True),
            auth_rate_limit_poll_seconds=60,
        )
        result = await loop.run_task(
            Task(account_id="acc_test", id="limited", capability="open_inbox", platform="tiktok_web")
        )
        self.assertFalse(result.success)
        self.assertIn("AUTH_RATE_LIMITED", result.error)
        self.assertEqual(observer.calls, 1)

    async def test_unknown_session_is_probed_once_then_business_loop_continues(self):
        unknown = Observation(
            screen=TikTokScreen.UNKNOWN, unknown=True,
            auth_state=AuthState.UNKNOWN,
        )
        logged_in = Observation(
            screen=TikTokScreen.INBOX, confidence=1,
            auth_state=AuthState.LOGGED_IN,
        )
        observer = SequenceObserver([unknown, logged_in])
        tools = ToolRegistry()

        async def check_auth():
            return {"success": True}

        tools.register("browser.check_auth", check_auth)
        adapter = TikTokWebAdapter(StubBrowser(), observer)
        loop = AgentLoop(
            StubScheduler(), observer, StubRouter(), StubPlanner(),
            PolicyEngine(tools.names), StubRecovery(), tools, adapter, Memory(),
            AgentState(auth_enforced=True),
            temporary_backoff_seconds=(0,),
        )
        result = await loop.run_task(
            Task(account_id="acc_test", id="valid", capability="open_inbox", platform="tiktok_web")
        )
        self.assertTrue(result.success)
        self.assertEqual(observer.calls, 2)

    async def test_unknown_session_never_repeats_protected_route_probe(self):
        unknown = Observation(
            screen=TikTokScreen.UNKNOWN, unknown=True,
            auth_state=AuthState.UNKNOWN,
        )
        observer = SequenceObserver([unknown, unknown])
        tools = ToolRegistry()
        probe_calls = 0

        async def check_auth():
            nonlocal probe_calls
            probe_calls += 1
            return {"success": True}

        tools.register("browser.check_auth", check_auth)
        adapter = TikTokWebAdapter(StubBrowser(), observer)
        loop = AgentLoop(
            StubScheduler(), observer, StubRouter(), StubPlanner(),
            PolicyEngine(tools.names), StubRecovery(), tools, adapter, Memory(),
            AgentState(auth_enforced=True), auth_poll_seconds=60,
            temporary_backoff_seconds=(0,),
        )
        result = await loop.run_task(
            Task(account_id="acc_test", id="unknown", capability="open_inbox", platform="tiktok_web")
        )
        self.assertFalse(result.success)
        self.assertIn("AUTH_REQUIRED", result.error)
        self.assertEqual(probe_calls, 1)
        self.assertEqual(observer.calls, 2)

    async def test_login_capability_is_rejected_without_observing_browser(self):
        adapter = TikTokWebAdapter(StubBrowser(), StubObserver())
        loop = AgentLoop(
            StubScheduler(), StubObserver(), StubRouter(), StubPlanner(),
            PolicyEngine([]), StubRecovery(), ToolRegistry(), adapter, Memory(),
            AgentState(auth_enforced=True),
        )
        result = await loop.run_task(
            Task(account_id="acc_test", id="no-login", capability="login", platform="tiktok_web")
        )
        self.assertFalse(result.success)
        self.assertIn("prohibited", result.error)


if __name__ == "__main__":
    unittest.main()


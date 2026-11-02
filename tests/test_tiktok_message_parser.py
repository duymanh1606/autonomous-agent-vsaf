"""The TikTok message parser, run in a real browser against real layout.

`direction` is decided from computed style and geometry, so a DOM without a
layout engine cannot test it. The extension profile is therefore loaded into a
real Chromium page over fixture threads that reproduce the shapes TikTok ships:
markers on the bubble, alignment only, position only, and a bubble that carries
no evidence at all.

The browser is Playwright's own build when it is present and matches the
installed package, otherwise the Google Chrome this project already requires
(see `_resolve_launcher`). Only when neither launches do these tests skip
rather than fail.
"""

import json
from types import SimpleNamespace
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.conversation_view import ConversationView, direction_debug_block
from platforms.tiktok_web.selectors import SELECTORS


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "tiktok_thread.html"
PROFILE = ROOT / "extension" / "platforms" / "tiktok.js"

# The profile registers itself on `globalThis.AgentPlatforms`; the page only has
# to provide that seam and to show one fixture at a time.
BOOTSTRAP = """
() => {
  globalThis.__profile = null;
  globalThis.AgentPlatforms = { register: (profile) => { globalThis.__profile = profile; } };
}
"""

# `hidden` is not enough: a hidden node is still found by `querySelector`, so
# every other fixture is removed from the document instead.
SHOW_CASE = """
(id) => {
  for (const surface of document.querySelectorAll('.surface')) {
    if (surface.id === id) surface.hidden = false;
    else surface.remove();
  }
  return document.querySelectorAll('.surface').length;
}
"""

READ = "() => globalThis.__profile.messages(40)"


def _resolve_launcher():
    """Pick the first browser that actually launches, and say why if none do.

    Playwright's own build comes first: it is self-contained and pinned to the
    installed package. The fallback is the Google Chrome this project already
    requires anyway - dashboard_api/processes.py spawns that same binary for
    every workspace - so a machine whose `~/.cache/ms-playwright` build does
    not match the installed playwright package (which silently skipped this
    whole file) now runs the parser against the exact browser production uses
    instead of testing nothing at all.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, "playwright is not installed"
    failures = []
    for options in ({}, {"channel": "chrome"}):
        try:
            with sync_playwright() as play:
                play.chromium.launch(**options).close()
            return options, ""
        except Exception as exc:
            failures.append(f"{options or 'bundled'}: {type(exc).__name__}")
    return None, "no launchable browser (" + "; ".join(failures) + ")"


LAUNCH, SKIP = _resolve_launcher()


@unittest.skipIf(SKIP, SKIP)
class MessageParser(unittest.TestCase):
    """One browser for the whole class; each test picks its own fixture."""

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls._play = sync_playwright().start()
        cls._browser = cls._play.chromium.launch(**LAUNCH)
        cls.page = cls._browser.new_page(viewport={"width": 1000, "height": 800})

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._play.stop()

    def read(self, case):
        """One fixture, alone in a freshly loaded page."""
        self.page.goto(FIXTURE.as_uri())
        self.assertEqual(self.page.evaluate(SHOW_CASE, case), 1)
        self.page.evaluate(BOOTSTRAP)
        self.page.add_script_tag(content=PROFILE.read_text())
        return self.page.evaluate(READ)

    # ------------------------------------------------------------- fixtures
    def test_alignment_only_thread_is_classified(self):
        """The shape that produced `directions=UNKNOWN:1` in production."""
        messages = self.read("case-a")
        self.assertEqual(
            [(item["text"], item["direction"]) for item in messages],
            [
                ("xin chào", "INBOUND"),
                ("Chào bạn, shop có thể giúp gì ạ?", "OUTBOUND"),
                ("áo này giá bao nhiêu", "INBOUND"),
            ],
        )
        self.assertTrue(all(item["direction_source"] == "layout" for item in messages))

    def test_thread_without_a_list_container_is_classified(self):
        messages = self.read("case-b")
        self.assertEqual(
            [(item["text"], item["direction"]) for item in messages],
            [
                ("alo shop ơi", "INBOUND"),
                ("Dạ em nghe ạ", "OUTBOUND"),
                ("còn size M không", "INBOUND"),
            ],
        )

    def test_position_is_the_last_resort(self):
        messages = self.read("case-c")
        self.assertEqual(
            [(item["text"], item["direction"]) for item in messages],
            [("ship về Hà Nội bao lâu", "INBOUND"), ("Dạ 2-3 ngày ạ", "OUTBOUND")],
        )
        self.assertEqual(
            [item["direction_source"] for item in messages], ["position", "position"]
        )

    def test_dom_semantics_outrank_the_layout(self):
        """The fixture aligns both bubbles the wrong way round on purpose."""
        messages = self.read("case-e")
        self.assertEqual(
            [(item["text"], item["direction"]) for item in messages],
            [("shop còn hàng không", "INBOUND"), ("Dạ còn ạ", "OUTBOUND")],
        )
        self.assertTrue(
            all(item["direction_source"] == "dom_semantics" for item in messages)
        )

    def test_platform_banner_is_not_a_message(self):
        """The banner that made a five-message thread read as one UNKNOWN."""
        self.assertEqual(self.read("case-d"), [])
        info = self.page.evaluate("() => globalThis.__profile.threadInfo()")
        self.assertEqual(info["notices"], 1)

    def test_banner_does_not_hide_the_customers_messages(self):
        """Production shape: banner in the thread, inbox rows next to it."""
        messages = self.read("case-g")
        self.assertEqual(
            [(item["text"], item["direction"]) for item in messages],
            [
                ("Chào cậu", "INBOUND"),
                ("Mua sicula", "INBOUND"),
                ("Dạ shop nghe ạ", "OUTBOUND"),
                ("Tôi cần tư vấn", "INBOUND"),
            ],
        )
        view = ConversationView.from_items(messages)
        self.assertEqual(
            [item.text for item in view.pending_inbound()], ["Tôi cần tư vấn"]
        )
        self.assertFalse(any(
            item["text"] == "Message request accepted. You can start chatting."
            for item in messages
        ))
        info = self.page.evaluate("() => globalThis.__profile.threadInfo()")
        self.assertEqual(info["notices"], 1)
        self.assertIn(
            info["container"], {"message_list", "chat_box", "structure"}
        )
        self.assertNotEqual(info["container"], "document")
        # A banner fills the thread; counting it as a bubble used to leave the
        # parser without a frame, which switches the position fallback off.
        self.assertIsNotNone(info["frame_width"])

    def test_a_stale_container_does_not_hide_the_live_thread(self):
        """`container=message_list candidates=1 notices=1` from the field.

        TikTok re-renders the conversation into a new list and leaves the old
        one in the page holding a banner. Reading only the first marked
        container reported six customer messages as an empty thread.
        """
        messages = self.read("case-h")
        self.assertEqual(
            [(item["text"], item["direction"]) for item in messages],
            [
                ("Hello", "INBOUND"),
                ("Tối cần tư vấn", "INBOUND"),
                ("Dạ shop nghe ạ", "OUTBOUND"),
                ("Mua sicula", "INBOUND"),
            ],
        )
        view = ConversationView.from_items(messages)
        self.assertEqual([item.text for item in view.pending_inbound()], ["Mua sicula"])

    def test_media_only_outbound_is_parsed_but_not_treated_as_draft(self):
        """A sent blob image is message evidence, not composer residue."""
        messages = self.read("case-i")
        self.assertEqual(
            [(item["text"], item["direction"]) for item in messages],
            [
                ("Cho mình xem ảnh nhé", "INBOUND"),
                ("", "OUTBOUND"),
                ("Đây là ảnh sản phẩm ạ", "OUTBOUND"),
            ],
        )
        self.assertEqual(messages[1]["media"][0]["kind"], "image")
        self.assertTrue(messages[1]["media"][0]["src"].startswith("blob:"))
        # The picture's own `alt` is not who sent it (reported live as
        # `sender=image message` on the shop's own image bubbles).
        self.assertEqual(messages[1]["sender"], "self")

        broad_matches = sum(
            self.page.locator(selector).count()
            for selector in SELECTORS["message_media_preview"]
        )
        draft_matches = sum(
            self.page.locator(selector).count()
            for selector in SELECTORS["message_media_draft_preview"]
        )
        self.assertGreater(broad_matches, 0)
        self.assertEqual(draft_matches, 0)

    def test_long_burst_of_short_messages_is_not_read_as_all_notices(self):
        """Regression: `candidates=N notices=N` for an 8-message unanswered
        burst - every real bubble was misread as a platform banner once the
        text-align/width heuristics in `isSystemNotice` stopped being gated
        on an unresolved direction."""
        messages = self.read("case-j")
        info = self.page.evaluate("() => globalThis.__profile.threadInfo()")
        self.assertEqual(info["notices"], 0)
        self.assertEqual(len(messages), 8)
        self.assertEqual(messages[0]["direction"], "OUTBOUND")
        self.assertTrue(all(item["direction"] == "INBOUND" for item in messages[1:]))
        view = ConversationView.from_items(messages)
        self.assertEqual(len(view.pending_inbound()), 7)

    def test_a_stale_container_bigger_than_the_live_one_does_not_win(self):
        """Regression: node count alone chose a stale, leftover
        `chat-message-list` over a live one that had genuinely fewer
        messages so far (still rendering, or simply a shorter real
        conversation) - a production thread kept reading one old sent
        image forever while the customer's real, new message sat in the
        live container next to it. DOM position (last one wins) is now the
        primary signal; node count only breaks ties at the same position."""
        messages = self.read("case-l")
        self.assertEqual(
            [(item["text"], item["direction"]) for item in messages],
            [("Còn hàng không shop", "INBOUND")],
        )
        info = self.page.evaluate("() => globalThis.__profile.threadInfo()")
        self.assertEqual(info["container_class"], "tiktok-hash-live thread")

    def test_a_smaller_stale_container_still_loses_to_the_live_one(self):
        """Control case for the fix above: when the stale container is the
        smaller one (the more common shape - see case-h), the live one must
        still win, exactly as before."""
        messages = self.read("case-k")
        self.assertEqual(
            [(item["text"], item["direction"]) for item in messages],
            [
                ("Chào shop", "INBOUND"),
                ("Dạ shop nghe ạ", "OUTBOUND"),
                ("Áo còn size M không ạ", "INBOUND"),
            ],
        )
        info = self.page.evaluate("() => globalThis.__profile.threadInfo()")
        self.assertEqual(info["container_class"], "tiktok-hash-live thread")

    def test_unresolvable_message_reports_its_dom(self):
        messages = self.read("case-f")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["direction"], "UNKNOWN")
        debug = messages[0]["debug"]
        self.assertIsNotNone(debug, "an unresolved direction must carry DOM evidence")
        for field in ("tag", "class", "parent_tag", "rect_left", "rect_right",
                      "container_center", "text"):
            self.assertIn(field, debug)
        block = direction_debug_block({
            "direction_debug": debug, "text": messages[0]["text"]
        })
        self.assertIn("tag=div", block)
        self.assertIn("container_center=", block)

    # ---------------------------------------------------------- the pipeline
    def test_parsed_thread_feeds_the_conversation_view(self):
        """What the parser returns is what the reply flow reads."""
        messages = self.read("case-a")
        for item in messages:
            self.assertTrue(item["id"], "every message needs an id")
            self.assertIn("sender", item)
        view = ConversationView.from_items(messages)
        pending = view.pending_inbound()
        self.assertEqual([item.text for item in pending], ["áo này giá bao nhiêu"])
        self.assertEqual(view.latest_inbound.text, "áo này giá bao nhiêu")
        self.assertEqual(view.latest_outbound.text, "Chào bạn, shop có thể giúp gì ạ?")
        self.assertFalse(view.already_replied())

    def test_media_and_json_shape_survive_the_round_trip(self):
        """What the extension serialises is what the runtime parses.

        This used to validate the payload against `bridge.protocol.Message`,
        the schema of the retired extension runtime. The live runtime reads
        the same payload through `ConversationView.from_dom_state`, so the
        round trip is asserted against that instead - one parser, the one
        that actually runs.
        """
        parsed = json.loads(json.dumps(self.read("case-a")))
        view = ConversationView.from_dom_state(
            SimpleNamespace(messages=parsed, thread_trusted=True)
        )

        self.assertEqual(len(view.messages), len(parsed))
        for message in view.messages:
            self.assertIn(message.direction, {"INBOUND", "OUTBOUND"})


if __name__ == "__main__":
    unittest.main()

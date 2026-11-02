"""A reply is never typed into a composer that still holds unsent media.

Live failure this covers (ws-bc391da5, 2026-09-21): a reply was aborted
mid-flow, the `platform.clear_message_draft` that follows an abort itself
failed, and 108 characters plus a staged image were left in the composer.
`fill()` replaces the leftover text on the next reply, but nothing in the send
path removes a file the agent did not stage - so a later text-only reply would
have sent that stale image with it.

The same incident started with one extension-bridge read missing its 500ms
budget while the page was busy uploading; the retry those tests cover is what
keeps a busy page from being mistaken for a page with no extension.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from platforms.tiktok_web.adapter import TikTokWebAdapter
from tools.browser import ChromeBrowserTool


class FakeNode:
    def __init__(self, *, visible=True, text="", files=0):
        self._visible = visible
        self._text = text
        self._files = files

    async def is_visible(self):
        return self._visible

    async def count(self):
        return 1

    @property
    def first(self):
        return self

    def nth(self, index):
        return self

    async def evaluate(self, script, *args):
        return self._files if "files" in script else self._text


class FakePage:
    """Resolves each selector to whatever the test put in `nodes`."""

    def __init__(self, nodes):
        self.nodes = nodes

    def locator(self, selector):
        return self.nodes.get(selector, FakeNode(visible=False))

    def is_closed(self):
        return False


def browser_with(page):
    tool = ChromeBrowserTool.__new__(ChromeBrowserTool)
    tool._page = page
    tool._browser = SimpleNamespace(is_connected=lambda: True)
    tool._extension_connected = False
    tool._extension_unavailable_logged = False
    tool._last_extension_sequence = 0
    tool._latest_extension_payload = None
    tool._extension_screen = None
    tool._extension_state_key = None
    tool._extension_url = None
    tool._extension_state_floor = 0
    return tool


class InspectMessageDraftTests(unittest.IsolatedAsyncioTestCase):
    """The read-only twin of `clear_message_draft`'s own verification."""

    EDITOR = '[contenteditable="true"][role="textbox"]'
    FILE_INPUT = 'input[type="file"][accept*="image" i]'

    async def test_an_empty_composer_reads_as_empty(self):
        browser = browser_with(FakePage({self.EDITOR: FakeNode(text="")}))

        draft = await browser.inspect_message_draft((self.EDITOR,), (self.FILE_INPUT,))

        self.assertTrue(draft["empty"])
        self.assertFalse(draft["media_present"])
        self.assertFalse(draft["text_present"])

    async def test_leftover_text_alone_is_not_media(self):
        browser = browser_with(FakePage({self.EDITOR: FakeNode(text="Dạ shop nghe ạ")}))

        draft = await browser.inspect_message_draft((self.EDITOR,), (self.FILE_INPUT,))

        self.assertFalse(draft["empty"])
        self.assertTrue(draft["text_present"])
        self.assertEqual(draft["characters"], len("Dạ shop nghe ạ"))
        # `fill()` replaces text; only media needs the draft cleared.
        self.assertFalse(draft["media_present"])

    async def test_a_staged_file_is_reported_as_media(self):
        browser = browser_with(FakePage({
            self.EDITOR: FakeNode(text=""),
            self.FILE_INPUT: FakeNode(files=1),
        }))

        draft = await browser.inspect_message_draft((self.EDITOR,), (self.FILE_INPUT,))

        self.assertTrue(draft["media_present"])
        self.assertEqual(draft["file_count"], 1)

    async def test_a_missing_composer_is_an_error_not_an_empty_draft(self):
        browser = browser_with(FakePage({}))

        with self.assertRaises(RuntimeError):
            await browser.inspect_message_draft((self.EDITOR,), (self.FILE_INPUT,))


class StaleDraftGateTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _adapter(draft, *, recipient="manh1"):
        verifier = SimpleNamespace(
            read_conversation=lambda: _state(recipient),
            confirm_conversation=lambda *a, **k: _confirmed(recipient),
        )
        return TikTokWebAdapter(
            browser=SimpleNamespace(inspect_message_draft=lambda *a, **k: _draft(draft)),
            observer=SimpleNamespace(), state_verifier=verifier,
        )

    async def test_staged_media_blocks_typing(self):
        adapter = self._adapter({"media_present": True, "file_count": 1})

        with self.assertRaises(RuntimeError) as raised:
            await adapter._require_clean_composer_for("manh1")

        message = str(raised.exception)
        self.assertIn("stale_draft", message)
        self.assertIn("file_count=1", message)

    async def test_a_clean_composer_passes_the_gate(self):
        adapter = self._adapter({"media_present": False, "file_count": 0})

        state = await adapter._require_clean_composer_for("manh1")

        self.assertEqual(state.active_chat, "manh1")

    async def test_leftover_text_alone_does_not_block_typing(self):
        adapter = self._adapter(
            {"media_present": False, "file_count": 0, "text_present": True}
        )

        await adapter._require_clean_composer_for("manh1")

    async def test_the_recipient_is_still_checked_first(self):
        """The wrong thread must be refused before the draft is even read:
        reading is harmless, but the order is what the gate promises."""
        adapter = self._adapter({"media_present": True}, recipient="other_customer")

        with self.assertRaises(RuntimeError) as raised:
            await adapter._require_clean_composer_for("manh1")

        self.assertIn("recipient_mismatch", str(raised.exception))


class FlakyBridge:
    """Times out `misses` times, then returns the payload."""

    def __init__(self, payload, misses):
        self.payload = payload
        self.misses = misses
        self.reads = 0

    @property
    def first(self):
        return self

    async def text_content(self, timeout=None):
        self.reads += 1
        if self.reads <= self.misses:
            raise TimeoutError("Locator.text_content: Timeout 500ms exceeded.")
        return self.payload


class BridgeReadRetryTests(unittest.IsolatedAsyncioTestCase):
    PAYLOAD = (
        '{"schema_version": 2, "platform": "tiktok_web", "sequence": 42,'
        ' "ui_state": "CONVERSATION", "elements": []}'
    )

    def _browser(self, bridge, *, connected):
        browser = browser_with(SimpleNamespace(
            locator=lambda selector: bridge, is_closed=lambda: False,
        ))
        browser._extension_connected = connected
        return browser

    async def test_one_missed_read_is_retried_once_the_extension_is_known(self):
        bridge = FlakyBridge(self.PAYLOAD, misses=1)
        browser = self._browser(bridge, connected=True)

        state = await browser._read_extension_state()

        self.assertEqual(bridge.reads, 2)
        self.assertIsNotNone(state)

    async def test_an_account_without_an_extension_pays_only_one_timeout(self):
        bridge = FlakyBridge(self.PAYLOAD, misses=5)
        browser = self._browser(bridge, connected=False)

        state = await browser._read_extension_state()

        self.assertEqual(bridge.reads, 1)
        self.assertIsNone(state)

    async def test_a_bridge_that_never_answers_still_falls_back(self):
        bridge = FlakyBridge(self.PAYLOAD, misses=5)
        browser = self._browser(bridge, connected=True)

        state = await browser._read_extension_state()

        self.assertEqual(bridge.reads, 2)
        self.assertIsNone(state)


async def _state(active):
    return SimpleNamespace(
        source="extension", active_chat=active, active_chat_present=True,
        composer_present=True, conversations=(), messages=(),
    )


async def _confirmed(active):
    return True, await _state(active)


async def _draft(draft):
    return draft


if __name__ == "__main__":
    unittest.main()

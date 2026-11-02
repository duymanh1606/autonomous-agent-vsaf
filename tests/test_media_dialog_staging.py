"""A video is staged once TikTok's "Send media" dialog shows it.

Reported by testing: a 3-second video never sent while pictures did. TikTok
shows a picked file in a dialog mounted outside the chatbox, and a video -
unlike a picture - is only ready once it has a preview, so the agent waited
for a preview inside the chatbox until it timed out. Run in a real Chromium,
since the check is the JavaScript itself.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from platforms.tiktok_web.selectors import SELECTORS
from tools.browser import ChromeBrowserTool

try:
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover
    async_playwright = None

# The shape of TikTok's page: the input in the chatbox, the dialog in <body>.
PAGE = """
<div data-e2e="dm-new-chatbox"><input type="file" accept="image/*,video/*"></div>
<div role="dialog" id="dialog" style="display:none">
  <video style="width:200px;height:100px;display:block"></video>
  <button data-e2e="dm-new-send-media-btn" DISABLED>Send</button>
</div>
"""


@unittest.skipIf(async_playwright is None, "playwright is not installed")
class MediaDialogStagingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        try:
            self.chrome = await self.playwright.chromium.launch()
        except Exception as error:  # no browser downloaded on this machine
            await self.playwright.stop()
            self.skipTest(f"Chromium is not available: {error}")
        self.page = await self.chrome.new_page()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.video = Path(tmp.name) / "clip.mp4"
        self.video.write_bytes(b"\x00\x00\x00\x18ftypmp42clip")
        self.browser = ChromeBrowserTool(media_stage_timeout_ms=2000)
        self.browser._page = self.page

    async def asyncTearDown(self):
        await self.chrome.close()
        await self.playwright.stop()

    async def stage(self, page, *, enabled):
        await self.page.set_content(page.replace("DISABLED", "" if enabled else "disabled"))
        field = self.page.locator('input[type="file"]')
        await field.set_input_files(str(self.video))
        await self.page.evaluate("document.getElementById('dialog').style.display = 'block'")
        return await self.browser._wait_for_media_staged(
            field, self.video.name, "video",
            SELECTORS["message_media_preview"], SELECTORS["message_media_uploading"],
            SELECTORS["message_media_error"], SELECTORS["message_media_dialog"],
            SELECTORS["message_media_confirm"],
        )

    async def test_a_video_in_the_media_dialog_is_staged(self):
        state = await self.stage(PAGE, enabled=True)
        self.assertTrue(state["preview"])

    async def test_not_while_tiktok_keeps_send_disabled(self):
        with self.assertRaisesRegex(RuntimeError, "did not stage the video"):
            await self.stage(PAGE, enabled=False)


if __name__ == "__main__":
    unittest.main()

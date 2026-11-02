"""Inbox pictures come off the server's own disk, or they do not display.

TikTok's own URLs cannot be embedded by the dashboard for three separate
reasons - `blob:` resolves only inside the Chrome tab that made it, DM
pictures sit on a `-private` host that answers 403 without the session
cookies, and even public sticker URLs are signed with `x-expires` about a
month out. `services.media_cache.MediaCache` downloads each picture once
through the agent's Chrome and hands back a local path; these tests pin the
bookkeeping around that, with the browser replaced by a fake.
"""

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.reply_service import PipelineReplyProvider
from services.media_cache import MediaCache
from tools.browser import ChromeBrowserTool


class FakeFetch:
    """Answers with `payload` and counts how often it was asked."""

    def __init__(self, payload=(b"\x89PNG-bytes", "image/png")):
        self.payload = payload
        self.calls = []

    async def __call__(self, url, *, max_bytes=0):
        self.calls.append(url)
        return self.payload


class RotatingFetch:
    """A different picture on each call."""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    async def __call__(self, url, *, max_bytes=0):
        self.calls.append(url)
        return self.payloads[min(len(self.calls) - 1, len(self.payloads) - 1)]


class MediaCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def _cache(self, fetch, **kwargs):
        return MediaCache(fetch, self.root, **kwargs)

    async def test_a_remote_picture_is_written_to_disk_and_gets_a_path(self):
        fetch = FakeFetch()
        cache = self._cache(fetch)

        resolved = await cache.localize([
            {"kind": "image", "src": "https://cdn.example.com/a/sticker.webp?x-expires=1"}
        ])

        path = Path(resolved[0]["path"])
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes(), b"\x89PNG-bytes")
        self.assertEqual(path.parent, self.root)
        # `src` stays: it says where the picture came from.
        self.assertIn("src", resolved[0])

    async def test_the_same_picture_re_signed_is_not_downloaded_again(self):
        """TikTok re-signs the same image on every render, so the query string
        must not be part of the identity - otherwise every scan writes one
        more copy of a picture that is already on disk."""
        fetch = FakeFetch()
        cache = self._cache(fetch)

        first = await cache.localize([
            {"kind": "image", "src": "https://cdn.example.com/a/sticker.webp?x-expires=1&x-signature=aa"}
        ])
        second = await cache.localize([
            {"kind": "image", "src": "https://cdn.example.com/a/sticker.webp?x-expires=2&x-signature=bb"}
        ])

        self.assertEqual(len(fetch.calls), 1)
        self.assertEqual(first[0]["path"], second[0]["path"])
        self.assertEqual(len(list(self.root.glob("*"))), 1)

    async def test_a_rotating_blob_url_maps_to_one_file(self):
        """Live regression (ws-bc391da5, 2026-09-21 11:16): a `blob:` URL is a
        new object URL on every render - the same just-sent picture was read as
        dom-8ea12b11, dom-2b2a429e and dom-b31e0c67 seconds apart, because the
        extension derives a media bubble's id from its `src`. Keyed on that
        URL, every scan wrote one more copy of the same image."""
        fetch = FakeFetch()
        cache = self._cache(fetch)

        first = await cache.localize([
            {"kind": "image", "src": "blob:https://www.tiktok.com/3efe4f21-aaaa"}
        ])
        second = await cache.localize([
            {"kind": "image", "src": "blob:https://www.tiktok.com/9c1ed5a9-bbbb"}
        ])

        self.assertEqual(first[0]["path"], second[0]["path"])
        self.assertEqual(len(list(self.root.glob("*"))), 1)
        # The bytes are the only identity a blob has, so it is re-read each
        # time - that fetch is in-page and costs no network.
        self.assertEqual(len(fetch.calls), 2)

    async def test_two_different_blobs_stay_two_files(self):
        cache = self._cache(RotatingFetch([
            (b"first-picture", "image/png"), (b"second-picture", "image/png"),
        ]))

        first = await cache.localize([{"kind": "image", "src": "blob:one"}])
        second = await cache.localize([{"kind": "image", "src": "blob:two"}])

        self.assertNotEqual(first[0]["path"], second[0]["path"])
        self.assertEqual(len(list(self.root.glob("*"))), 2)

    async def test_an_agent_attachment_is_never_downloaded(self):
        """It is already a file on this disk - the product catalogue one."""
        fetch = FakeFetch()
        cache = self._cache(fetch)

        resolved = await cache.localize([
            {"kind": "image", "path": "/srv/products/sp001/images/a.png", "name": "a.png"}
        ])

        self.assertEqual(fetch.calls, [])
        self.assertEqual(resolved[0]["path"], "/srv/products/sp001/images/a.png")

    async def test_a_failed_download_leaves_the_item_untouched(self):
        """The dashboard then falls back to `src` while it still works; losing
        a picture is never allowed to lose the message it came with."""
        cache = self._cache(FakeFetch((b"", "")))

        resolved = await cache.localize([
            {"kind": "image", "src": "https://cdn.example.com/a/gone.webp"}
        ])

        self.assertNotIn("path", resolved[0])
        self.assertEqual(resolved[0]["src"], "https://cdn.example.com/a/gone.webp")
        self.assertEqual(list(self.root.glob("*")), [])

    async def test_a_blob_with_no_content_type_is_stored_by_sniffing_it(self):
        """Live regression (ws-bc391da5, 2026-09-21 11:14-11:18): ten pictures
        were downloaded and then dropped as `unsupported_content_type=''`.
        `fetch()` on a `blob:` URL reports the Blob's own type, which is empty
        when the page made it without one, and a blob URL has no suffix to
        fall back to - so the bytes already in hand were thrown away."""
        png = b"\x89PNG\r\n\x1a\n" + b"body"
        cache = self._cache(FakeFetch((png, "")))

        resolved = await cache.localize([
            {"kind": "image", "src": "blob:https://www.tiktok.com/3efe4f21"}
        ])

        path = Path(resolved[0]["path"])
        self.assertTrue(path.name.endswith(".png"))
        self.assertEqual(path.read_bytes(), png)

    async def test_the_content_type_still_wins_over_the_bytes(self):
        """Sniffing is the last resort, not a second opinion."""
        cache = self._cache(FakeFetch((b"\x89PNG\r\n\x1a\nbody", "image/webp")))

        resolved = await cache.localize([
            {"kind": "image", "src": "https://cdn.example.com/a"}
        ])

        self.assertTrue(resolved[0]["path"].endswith(".webp"))

    async def test_a_content_type_that_is_not_a_picture_is_not_stored(self):
        """A 200 with an HTML error page must not become an <img> the
        dashboard serves from our own origin."""
        cache = self._cache(FakeFetch((b"<html>403</html>", "text/html")))
        # Neither the header, the URL nor the bytes say picture.

        resolved = await cache.localize([
            {"kind": "image", "src": "https://cdn.example.com/a/blocked"}
        ])

        self.assertNotIn("path", resolved[0])
        self.assertEqual(list(self.root.glob("*")), [])

    async def test_the_extension_falls_back_to_the_url_when_the_type_is_missing(self):
        cache = self._cache(FakeFetch((b"webp-bytes", "")))

        resolved = await cache.localize([
            {"kind": "image", "src": "https://cdn.example.com/a/sticker.awebp?x=1"}
        ])

        self.assertTrue(resolved[0]["path"].endswith(".webp"))

    async def test_an_oversized_picture_is_skipped(self):
        cache = self._cache(FakeFetch((b"x" * 50, "image/png")), max_file_bytes=10)

        resolved = await cache.localize([
            {"kind": "image", "src": "https://cdn.example.com/a/big.png"}
        ])

        self.assertNotIn("path", resolved[0])

    async def test_the_budget_evicts_the_least_recently_used_file(self):
        cache = self._cache(
            FakeFetch((b"x" * 100, "image/png")), max_total_bytes=250,
        )

        paths = []
        for index in range(3):
            resolved = await cache.localize([
                {"kind": "image", "src": f"https://cdn.example.com/a/{index}.png"}
            ])
            paths.append(Path(resolved[0]["path"]))

        # Three 100-byte files do not fit in 250, so one had to go - and the
        # one just downloaded is never it.
        self.assertTrue(paths[2].is_file())
        self.assertEqual(
            sum(1 for path in paths if path.is_file()), 2,
        )
        self.assertLessEqual(
            sum(path.stat().st_size for path in self.root.glob("*") if path.is_file()),
            250,
        )

    async def test_one_broken_item_does_not_lose_the_others(self):
        cache = self._cache(FakeFetch())

        resolved = await cache.localize([
            "not a dict",
            {"kind": "image", "src": "https://cdn.example.com/a/ok.png"},
        ])

        self.assertEqual(len(resolved), 2)
        self.assertIn("path", resolved[1])

    async def test_a_partial_write_never_becomes_a_served_file(self):
        """`_existing` must not hand out the `.part` file a crashed download
        left behind."""
        cache = self._cache(FakeFetch())
        url = "https://cdn.example.com/a/sticker.png"
        stem = MediaCache.cache_key(url)[:32]
        (self.root / f"{stem}.png.part").write_bytes(b"half")

        resolved = await cache.localize([{"kind": "image", "src": url}])

        self.assertTrue(resolved[0]["path"].endswith(".png"))
        self.assertEqual(Path(resolved[0]["path"]).read_bytes(), b"\x89PNG-bytes")


class ReplyServiceLocalizationTests(unittest.IsolatedAsyncioTestCase):
    """The pictures are localized *before* the rows are written, so the
    `path` the dashboard needs is part of the message from its first version
    onwards instead of something a later pass has to go back and add."""

    @staticmethod
    def _provider(media_cache):
        provider = PipelineReplyProvider.__new__(PipelineReplyProvider)
        provider.media_cache = media_cache
        return provider

    async def test_every_bubble_gets_its_local_path(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        cache = MediaCache(FakeFetch(), Path(directory.name))
        message = SimpleNamespace(
            text="", direction="INBOUND", order=3,
            media=[{"kind": "image", "src": "https://cdn.example.com/a.png"}],
        )

        localized = await self._provider(cache)._localize_media([message])

        self.assertTrue(localized[0].media[0]["path"].endswith(".png"))

    async def test_without_a_cache_the_messages_pass_straight_through(self):
        message = SimpleNamespace(
            text="", direction="INBOUND", order=3,
            media=[{"kind": "image", "src": "https://cdn.example.com/a.png"}],
        )

        localized = await self._provider(None)._localize_media([message])

        self.assertIs(localized[0], message)
        self.assertNotIn("path", message.media[0])


class MediaUrlGuardTests(unittest.TestCase):
    """What the server is allowed to fetch. `src` is read off the page's DOM,
    so it is attacker-influenced in the same way any scraped URL is."""

    def test_https_and_blob_are_allowed(self):
        self.assertTrue(ChromeBrowserTool._media_url_allowed("https://cdn.example.com/a.png"))
        self.assertTrue(ChromeBrowserTool._media_url_allowed("blob:https://www.tiktok.com/abc"))

    def test_plain_http_and_other_schemes_are_refused(self):
        for url in (
            "http://cdn.example.com/a.png",
            "file:///etc/passwd",
            "data:image/png;base64,AAA",
            "",
        ):
            self.assertFalse(ChromeBrowserTool._media_url_allowed(url), url)

    def test_the_server_own_network_is_refused(self):
        for host in (
            "localhost", "127.0.0.1", "10.0.0.5", "192.168.1.10",
            "169.254.169.254", "172.16.0.9", "172.31.255.255",
        ):
            self.assertFalse(
                ChromeBrowserTool._media_url_allowed(f"https://{host}/a.png"), host
            )

    def test_a_public_address_that_merely_starts_with_172_is_allowed(self):
        self.assertTrue(ChromeBrowserTool._media_url_allowed("https://172.32.0.1/a.png"))


class FetchMediaBytesTests(unittest.IsolatedAsyncioTestCase):
    """The two transports `fetch_media_bytes` picks between."""

    def _browser(self, *, request=None, evaluate=None):
        tool = ChromeBrowserTool.__new__(ChromeBrowserTool)
        tool.base_url = "https://www.tiktok.com"
        tool.timeout_ms = 5000
        tool._browser = SimpleNamespace(is_connected=lambda: True)
        tool._page = SimpleNamespace(
            is_closed=lambda: False,
            request=SimpleNamespace(get=request),
            evaluate=evaluate,
        )
        return tool

    async def test_a_cdn_url_goes_through_the_browser_request_with_the_session(self):
        seen = {}

        async def get(url, headers=None, timeout=None):
            seen.update(url=url, headers=headers)
            return SimpleNamespace(
                ok=True, status=200, headers={"content-type": "image/webp; charset=x"},
                body=_bytes(b"webp"),
            )

        data, content_type = await self._browser(request=get).fetch_media_bytes(
            "https://p0-tiktok-dm-private-sg.tiktok.com/a.image"
        )

        self.assertEqual(data, b"webp")
        self.assertEqual(content_type, "image/webp")
        # The referer is what a DM CDN checks alongside the cookies.
        self.assertEqual(seen["headers"], {"referer": "https://www.tiktok.com/"})

    async def test_a_403_is_reported_as_nothing_downloaded(self):
        async def get(url, headers=None, timeout=None):
            return SimpleNamespace(ok=False, status=403, headers={}, body=_bytes(b""))

        data, content_type = await self._browser(request=get).fetch_media_bytes(
            "https://p0-tiktok-dm-private-sg.tiktok.com/a.image"
        )

        self.assertEqual((data, content_type), (b"", ""))

    async def test_a_blob_url_is_read_inside_the_page(self):
        async def evaluate(script, url):
            self.assertIn("fetch(url)", script)
            return {"data": "YWJj", "type": "image/png", "size": 3}

        data, content_type = await self._browser(evaluate=evaluate).fetch_media_bytes(
            "blob:https://www.tiktok.com/abc"
        )

        self.assertEqual(data, b"abc")
        self.assertEqual(content_type, "image/png")

    async def test_a_transport_error_is_not_raised_at_the_caller(self):
        async def get(url, headers=None, timeout=None):
            raise TimeoutError("timed out")

        data, _ = await self._browser(request=get).fetch_media_bytes(
            "https://cdn.example.com/a.png"
        )

        self.assertEqual(data, b"")

    async def test_a_refused_url_is_never_fetched(self):
        async def get(url, headers=None, timeout=None):
            raise AssertionError("the guard let a private address through")

        data, _ = await self._browser(request=get).fetch_media_bytes(
            "https://169.254.169.254/latest/meta-data/"
        )

        self.assertEqual(data, b"")


def _bytes(value):
    async def body():
        return value
    return body


if __name__ == "__main__":
    unittest.main()

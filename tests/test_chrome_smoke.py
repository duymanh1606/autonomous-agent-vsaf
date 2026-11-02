"""The seam every field bug came from, exercised against a real Chrome.

Four defects reached production through the same joint: the extension reads
the DOM, the agent builds a model of it, and the two disagreed.

* a thread whose newest rendered message was ours was read as "cannot read
  this thread" and escalated to a person after 30 scans;
* one missed bridge read was reported as "the wrong conversation is open" and
  aborted a correct reply;
* the same reply was written to the database twice, once with the local
  attachment path and once with TikTok's own URL;
* a just-sent picture's `blob:` URL changes on every render, so the media
  cache stored one copy of it per scan.

None of them is reachable by a test with a stubbed DOM, because in a stub the
agent's model *is* the fixture. So this drives the real extension code in a
real Chrome over the real CDP attach, against `tests/fixtures/fake_tiktok`.

What is real here: Chrome itself, the CDP connection, `ChromeBrowserTool`,
`extension/`'s own content scripts (read from its manifest, in manifest
order), `StateParser`, `ConversationView`, `TikTokWebAdapter` and the
grounding stack that clicks and types.

What is not: the extension's *installation*. Modern Chrome refuses
`--load-extension` for an unpacked extension, which is why production packs a
signed .crx and installs a machine-level policy
(`scripts/pack_extension.sh`, `deploy/install_extension_policy.sh`). That
needs root, so here the same scripts are injected as init scripts instead, into
a Chrome started with `--disable-extensions` so that a machine which *has* had
that policy installed cannot quietly answer with the real extension. The
loading mechanism is a per-machine install concern; the DOM contract is the
part that drifts, and that is what this covers.

Skipped when Chrome or openssl is missing, so the ordinary suite is
unaffected. TLS and `--host-resolver-rules` are needed because the extension
only injects on `https://*.tiktok.com/*`.
"""

import asyncio
import json
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grounding.action_executor import ActionExecutor
from grounding.dom_resolver import DomResolver
from models.conversation_view import ConversationView
from perception.dom_observer import DomObserver
from perception.state_parser import StateParser
from platforms.tiktok.screens import TikTokScreen
from platforms.tiktok_web.adapter import TikTokWebAdapter
from platforms.tiktok_web.selectors import SELECTORS, TARGET_SELECTORS
from core.state_verifier import StateVerifier
from tools.browser import ChromeBrowserTool


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "fake_tiktok"
CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")


def _chrome() -> str | None:
    for name in CHROME_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _requirements_missing() -> str:
    if _chrome() is None:
        return "no Chrome/Chromium on this machine"
    if shutil.which("openssl") is None:
        return "openssl is needed for the fixture's TLS certificate"
    return ""


@unittest.skipIf(_requirements_missing(), _requirements_missing())
class FakeTikTokChromeTests(unittest.IsolatedAsyncioTestCase):
    """One Chrome per test: a fresh profile is cheaper than shared state."""

    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="chrome-smoke-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._serve_fixture_over_tls()
        self.browser = None
        self.chrome = None

    async def asyncTearDown(self):
        if self.browser is not None:
            await self.browser.close()
        if self.chrome is not None:
            self.chrome.terminate()
            try:
                self.chrome.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.chrome.kill()
        self.httpd.shutdown()

    # ------------------------------------------------------------- fixture
    def _serve_fixture_over_tls(self):
        certificate, key = self.tmp / "cert.pem", self.tmp / "key.pem"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(key), "-out", str(certificate), "-days", "2",
             "-subj", "/CN=www.tiktok.com",
             "-addext", "subjectAltName=DNS:www.tiktok.com"],
            check=True, capture_output=True,
        )
        self.https_port = _free_port()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.https_port), _FixtureHandler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(certificate), str(key))
        self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        Thread(target=self.httpd.serve_forever, daemon=True).start()

    async def _open(self, state: str):
        """Chrome on the fixture in `state`, with the agent attached to it."""
        cdp_port = _free_port()
        url = f"https://www.tiktok.com/messages.html?state={state}"
        self.chrome = subprocess.Popen(
            [_chrome(), "--headless=new", f"--remote-debugging-port={cdp_port}",
             f"--user-data-dir={self.tmp / 'profile'}",
             # Chrome resolves TikTok's name to the fixture server, so the
             # page really is served from https://www.tiktok.com.
             f"--host-resolver-rules=MAP www.tiktok.com 127.0.0.1:{self.https_port}",
             "--ignore-certificate-errors", "--no-first-run",
             "--no-default-browser-check", "--disable-gpu",
             # A dev machine that ran deploy/install_extension_policy.sh has a
             # machine-level policy force-installing the packed extension into
             # every profile, this throwaway one included. It would then be the
             # real extension doing the work here while the injected scripts
             # below are dead code - the test passes for a reason CI does not
             # have, and the injected path it means to cover goes unexercised.
             "--disable-extensions",
             "--window-size=1100,680", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        await self._wait_for_cdp(cdp_port)

        self.browser = ChromeBrowserTool(
            cdp_url=f"http://127.0.0.1:{cdp_port}",
            screenshot_dir=str(self.tmp / "screenshots"),
            base_url="https://www.tiktok.com",
            allowed_media_root=str(self.tmp),
        )
        await self.browser.start()
        await self.browser._page.add_init_script(_content_scripts())
        await self.browser._page.reload()
        await self._wait_for_bridge()

    @staticmethod
    async def _wait_for_cdp(port, timeout=30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/version", timeout=1
                )
                return
            except (urllib.error.URLError, OSError):
                await asyncio.sleep(0.2)
        raise AssertionError("Chrome never opened its debugging port")

    async def _wait_for_bridge(self, timeout=20.0):
        """Wait until the extension has published a state, then return it."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = await self.browser.snapshot()
            if snapshot.get("extension_state"):
                return snapshot
            await asyncio.sleep(0.3)
        raise AssertionError(
            "the extension never published a state: " + await self._diagnosis()
        )

    async def _diagnosis(self):
        """Say which half broke. "Never published" has two very different
        causes - the agent attached to a page the scripts cannot run on (a
        blank tab, a failed fixture load), or the scripts ran and bailed - and
        without this the CI failure looks identical either way.
        """
        try:
            return json.dumps(await self.browser._page.evaluate(
                """() => ({
                    url: location.href,
                    ready: document.readyState,
                    body_text: (document.body?.innerText || '').trim().length,
                    scripts_loaded: {
                        platforms: typeof globalThis.AgentPlatforms,
                        observer: typeof globalThis.AgentObserver,
                        executor: typeof globalThis.AgentExecutor
                    },
                    platform: globalThis.AgentPlatforms?.match()?.id || null,
                    bridge: Boolean(document.getElementById(
                        'autonomous-agent-perception-v1'))
                })"""
            ), ensure_ascii=False)
        except Exception as exc:  # the page itself is unreachable
            return f"could not even read the page: {type(exc).__name__}: {exc}"

    async def _parsed(self):
        return StateParser().parse(await self.browser.snapshot())

    # -------------------------------------------------------- perception
    async def test_an_unanswered_thread_is_read_as_one(self):
        await self._open("unanswered")

        state = await self._parsed()

        self.assertEqual(state.screen, TikTokScreen.CONVERSATION)
        self.assertEqual(state.source, "extension")
        self.assertEqual(state.active_chat, "manh1")
        self.assertTrue(state.composer_present)
        self.assertTrue(state.thread_trusted)
        # The directions come from the row's class word, the way TikTok
        # carries them - not from screen position.
        view = ConversationView.from_dom_state(state)
        self.assertEqual(
            [(item.direction, item.text) for item in view.messages],
            [
                ("INBOUND", "chào shop"),
                ("OUTBOUND", "Dạ em nghe ạ"),
                ("INBOUND", "áo này còn size M không?"),
            ],
        )
        self.assertEqual(
            [item.text for item in view.pending_inbound()],
            ["áo này còn size M không?"],
        )
        self.assertFalse(view.already_replied())

    async def test_an_answered_thread_has_nothing_pending(self):
        await self._open("answered")

        view = ConversationView.from_dom_state(await self._parsed())

        self.assertEqual(view.pending_inbound(), ())
        self.assertTrue(view.already_replied())

    async def test_a_thread_of_only_our_own_messages_is_readable(self):
        """The live bug: this shape was read as "cannot read this thread" and,
        after 30 scans, escalated a conversation the agent had answered."""
        await self._open("outbound_only")

        state = await self._parsed()
        view = ConversationView.from_dom_state(state)

        # Readable - it parsed, it is trusted, and it says what it is.
        self.assertTrue(state.thread_trusted)
        self.assertEqual([item.direction for item in view.messages], ["OUTBOUND"])
        # Nothing to answer, and no inbound to mistake for one.
        self.assertIsNone(view.latest_inbound)
        self.assertEqual(view.pending_inbound(), ())

    async def test_the_conversation_list_carries_the_unread_flag(self):
        await self._open("unanswered")

        state = await self._parsed()

        rows = {row.name: row for row in state.conversations}
        self.assertIn("manh1", rows)
        self.assertTrue(rows["manh1"].unread)
        self.assertFalse(rows["helloworld!!"].unread)

    # ------------------------------------------------------------- action
    async def test_a_reply_typed_and_sent_lands_in_the_thread(self):
        """Focus, type, send and then prove it from the DOM - the same four
        steps, through the same adapter and the same gates, that a real reply
        goes through."""
        await self._open("unanswered")
        adapter = self._adapter()
        before = await adapter.state_verifier.read_conversation()
        inbound = ConversationView.from_dom_state(before).latest_inbound

        await adapter.focus_message_input(recipient="manh1")
        await adapter.input_text("Dạ còn size M ạ", recipient="manh1")
        result = await adapter.send_message(
            text="Dạ còn size M ạ", recipient="manh1", conversation_id="manh1",
        )

        self.assertTrue(result.get("success", True))
        confirmed, _, outbound = await adapter.state_verifier.confirm_reply_after_inbound(
            inbound_message_id=inbound.id, text="Dạ còn size M ạ",
        )
        self.assertTrue(confirmed)
        self.assertEqual(outbound.text, "Dạ còn size M ạ")
        # And the page itself shows it, not just our reading of it.
        self.assertIn(
            "Dạ còn size M ạ",
            await self.browser._page.inner_text('[data-e2e="chat-message-list"]'),
        )

    async def test_the_gate_refuses_to_type_to_the_wrong_recipient(self):
        """The header says `manh1`; anything else must not be typed to."""
        await self._open("unanswered")
        adapter = self._adapter()

        with self.assertRaises(RuntimeError) as raised:
            await adapter.input_text("xin chào", recipient="somebody_else")

        message = str(raised.exception)
        self.assertIn("recipient_mismatch", message)
        self.assertIn("active=manh1", message)
        # Nothing was typed.
        self.assertEqual(
            (await self.browser._page.inner_text("#editor")).strip(), ""
        )

    def _adapter(self):
        """The adapter as main.py wires it, minus vision, database and LLM."""
        dom_observer = DomObserver(self.browser, StateParser())
        adapter = TikTokWebAdapter(
            self.browser, observer=None, base_url="https://www.tiktok.com",
            dom_resolver=DomResolver(self.browser, SELECTORS, TARGET_SELECTORS),
            action_executor=ActionExecutor(self.browser),
            dom_observer=dom_observer,
        )
        adapter.state_verifier = StateVerifier(adapter, dom_observer)
        return adapter


def _content_scripts() -> str:
    """The extension's own content scripts, in the order its manifest lists.

    Read from the manifest rather than hardcoded, so a script added to the
    extension is part of this test without anyone remembering to add it.
    """
    manifest = json.loads((ROOT / "extension" / "manifest.json").read_text())
    # The scripts normally run in an isolated world with the extension APIs
    # available; only `runtime.sendMessage` and `runtime.onMessage` are
    # actually called by content.js.
    #
    # Chrome puts a `window.chrome` on every page already ({loadTimes, csi,
    # app}) and only adds `runtime` to it inside a real extension world, so
    # this has to merge rather than fill in a missing object. `x = x || {...}`
    # never applied, content.js threw a TypeError on its
    # `chrome.runtime.onMessage` listener - which it registers at load, above
    # boot() - and the bridge was never published.
    parts = [
        """(() => {
          const runtime = {
            sendMessage: async () => ({}),
            onMessage: {addListener: () => {}},
            getURL: (path) => path,
            id: 'chrome-smoke-test'
          };
          if (!window.chrome) { window.chrome = {runtime}; return; }
          if (window.chrome.runtime) return;
          try { window.chrome.runtime = runtime; } catch (_error) {}
          if (!window.chrome.runtime) {
            Object.defineProperty(window.chrome, 'runtime', {
              value: runtime, writable: true, configurable: true
            });
          }
        })();"""
    ]
    for entry in manifest.get("content_scripts", ()):
        for relative in entry.get("js", ()):
            parts.append((ROOT / "extension" / relative).read_text())
    return "\n;\n".join(parts)


class _FixtureHandler(SimpleHTTPRequestHandler):
    """Serves the fixture directory, and keeps its request log out of the
    test report."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FIXTURE_DIR), **kwargs)

    def log_message(self, *args):
        return


if __name__ == "__main__":
    unittest.main()

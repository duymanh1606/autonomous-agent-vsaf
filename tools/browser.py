import asyncio
import base64
import hashlib
import json
import logging
import re
import subprocess
from pathlib import Path
from time import monotonic, time_ns
from urllib.parse import urlparse

from models.auth import AuthState, detect_auth_state
from models.ui_errors import TargetNotFound
from platforms.tiktok_web.ui_states import UI_STATES, normalize_ui_state


logger = logging.getLogger(__name__)


class ChromeBrowserTool:
    """Attach to a user-managed Dedicated Chrome through CDP."""

    _TRANSIENT_NAVIGATION_ERRORS = (
        "execution context was destroyed",
        "cannot find context with specified id",
        "frame was detached",
    )

    def __init__(
        self,
        cdp_url="http://127.0.0.1:29222",
        screenshot_dir="data/screenshots",
        base_url="https://www.tiktok.com",
        timeout_ms=15000,
        screenshot_delay_seconds=7,
        max_screenshots_per_run=9,
        allowed_media_root="data/products",
        extra_media_roots=(),
        media_stage_timeout_ms=60000,
        allowed_media_mime_types=(),
        max_image_size_mb=10,
        max_video_size_mb=25,
        max_dm_video_duration_seconds=60,
        auto_login_enabled=False,
        auto_login_max_attempts=2,
        auto_login_cooldown_seconds=300,
        auto_login_post_submit_wait_seconds=3,
        auto_login_fedcm_settle_seconds=2,
        egw_workspace_id="",
        egw_token="",
    ):
        self.cdp_url = cdp_url
        # egress-ip-gateway credentials. Chrome is attached over CDP, not
        # launched by Playwright, so `launch(proxy=...)` is unavailable; the
        # 407 challenge is answered through the CDP Fetch domain instead
        # (see _install_proxy_auth). Both empty = no proxy, nothing installed.
        self.egw_workspace_id = str(egw_workspace_id or "").strip()
        self.egw_token = str(egw_token or "").strip()
        self._proxy_auth_session = None
        self.screenshot_dir = Path(screenshot_dir)
        self.base_url = base_url.rstrip("/")
        self.timeout_ms = int(timeout_ms)
        self.screenshot_delay_seconds = max(0.0, float(screenshot_delay_seconds))
        self.max_screenshots_per_run = max(0, int(max_screenshots_per_run))
        self.allowed_media_root = Path(allowed_media_root).resolve()
        # Every directory a sent file may come from: the product catalogue,
        # plus (in production) the workspace's own uploads directory, where
        # the dashboard puts a file an operator attaches in the chat. Nothing
        # else on the machine can ever be staged into a customer's thread.
        self.allowed_media_roots = (self.allowed_media_root,) + tuple(
            Path(root).resolve() for root in extra_media_roots
        )
        self.media_stage_timeout_ms = max(1000, int(media_stage_timeout_ms))
        self.allowed_media_mime_types = frozenset(
            str(value).strip().casefold() for value in allowed_media_mime_types if value
        )
        self.max_image_size_bytes = max(1, int(max_image_size_mb)) * 1024 * 1024
        self.max_video_size_bytes = max(1, int(max_video_size_mb)) * 1024 * 1024
        self.max_dm_video_duration_seconds = max(
            1.0, float(max_dm_video_duration_seconds)
        )
        self.auto_login_enabled = bool(auto_login_enabled)
        self.auto_login_max_attempts = max(1, int(auto_login_max_attempts))
        self.auto_login_cooldown_seconds = max(1.0, float(auto_login_cooldown_seconds))
        self.auto_login_post_submit_wait_seconds = max(
            0.0, float(auto_login_post_submit_wait_seconds)
        )
        self.auto_login_fedcm_settle_seconds = max(
            0.0, float(auto_login_fedcm_settle_seconds)
        )
        self._auto_login_attempts = 0
        self._auto_login_next_at = 0.0
        self._fedcm_session = None
        self._fedcm_dialog = None
        self._fedcm_dialog_shown_at = 0.0
        self._fedcm_unavailable_logged = False
        self._google_flow_active = False
        self._start_lock = asyncio.Lock()
        self._screenshot_lock = asyncio.Lock()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._allowed_host = urlparse(self.base_url).hostname
        self._visual_targets = {}
        self._visual_screenshot = None
        self._visual_url = None
        self._visual_target_threshold = 0.85
        self._last_send_evidence = None
        self._last_extension_sequence = 0
        self._latest_extension_payload = None
        self._extension_connected = False
        self._extension_unavailable_logged = False
        self._extension_screen = None
        self._extension_state_key = None
        self._extension_url = None
        self._extension_state_floor = 0
        self._force_dom_snapshot_once = False
        self._perception_wait_result = None
        self._run_started_at = None
        self._screenshot_attempt_count = 0

    def begin_task(self):
        """Start a fresh, bounded screenshot budget for one agent task."""
        self._run_started_at = monotonic()
        self._screenshot_attempt_count = 0
        self._visual_targets = {}
        self._visual_screenshot = None
        self._visual_url = None

    async def start(self):
        async with self._start_lock:
            if self._browser and self._browser.is_connected() and self._page and not self._page.is_closed():
                return {
                    "success": True,
                    "url": self._page.url,
                    "tiktok_attached": self._is_tiktok_url(self._page.url),
                }
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise RuntimeError("Playwright is not installed. Run: uv sync") from exc

            self._playwright = await async_playwright().start()
            try:
                self._browser = await self._playwright.chromium.connect_over_cdp(self.cdp_url)
                self._context, self._page = await self._select_context_and_page()
                self._context.set_default_timeout(self.timeout_ms)
                # Chrome no longer launches with --disable-notifications (see
                # dashboard_api/processes.py's _chrome_command), since TikTok's
                # own message-request queue does not refresh live and a real
                # Notification might be the only signal available for it - see
                # platforms/tiktok/inbox_watcher.py's _maybe_reload_for_new_
                # activity. Granting the permission up front is what that flag
                # used to do implicitly (never showing the browser's own
                # permission-prompt banner, which would otherwise sit on top
                # of the page and could block a click); best-effort because a
                # context Playwright merely attached to over CDP (rather than
                # created itself) does not always support this call.
                try:
                    await self._context.grant_permissions(
                        ["notifications"], origin="https://www.tiktok.com"
                    )
                except Exception:
                    logger.warning(
                        "[EXT] Could not pre-grant the notifications permission for tiktok.com",
                        exc_info=True,
                    )
                if self.egw_enabled:
                    # Before anything else touches the network: Chrome loaded
                    # the initial TikTok URL before we attached, so that first
                    # load hit the proxy's 407 with nobody to answer it. Once
                    # the handler is in place, one same-URL reload (the one
                    # sanctioned exception to "no navigation", see
                    # reload_page) replays the load through the handler.
                    await self._install_proxy_auth()
                    await self._page.reload(wait_until="domcontentloaded")
                await self._install_fedcm_bridge()
                try:
                    await self._install_extension_event_bridge()
                except Exception as bridge_exc:
                    logger.warning(
                        "[EXT] Bridge initialization failed error_type=%s - fallback to browser DOM",
                        type(bridge_exc).__name__, exc_info=True,
                    )
                    self._extension_unavailable_logged = True
                self._run_started_at = monotonic()
            except Exception as exc:
                await self._disconnect()
                raise RuntimeError(f"Chrome CDP unavailable at {self.cdp_url}: {exc}") from exc
            return {
                "success": True,
                "url": self._page.url,
                "tiktok_attached": self._is_tiktok_url(self._page.url),
            }

    # Chrome answers on its debugging port before it has created the tab it
    # was launched with, so an attach landing in that window sees no TikTok
    # tab at all. Opening a blank page there is the worst outcome available:
    # the agent then drives about:blank, the extension never injects (it only
    # matches tiktok.com), every observation falls back to the raw browser DOM
    # and the recipient gate reads the missing header as "the wrong thread is
    # open". Waiting for the tab costs nothing when it is already there.
    TAB_WAIT_SECONDS = 20.0

    async def _select_context_and_page(self):
        deadline = monotonic() + self.TAB_WAIT_SECONDS
        candidates = []
        while True:
            contexts = self._browser.contexts
            if not contexts:
                raise RuntimeError("Dedicated Chrome exposes no browser context")
            candidates.clear()
            for context in contexts:
                for page in context.pages:
                    if self._is_tiktok_url(page.url):
                        candidates.append((context, page))
            # Prefer a rendered TikTok tab over an old splash/loading tab. Never
            # inspect cookies or page storage while selecting the tab.
            for context, page in reversed(candidates):
                try:
                    state = await page.evaluate(
                        "() => ({text: (document.body?.innerText || '').trim().length, "
                        "nodes: document.querySelectorAll('a,button,input,[data-e2e]').length})"
                    )
                    if state["text"] > 0 and state["nodes"] >= 3:
                        return context, page
                except Exception:  # noqa: BLE001 - a tab closing mid-probe; the next one is tried
                    continue
            if monotonic() >= deadline:
                break
            # Either no tab yet, or one that has not rendered; both are worth
            # the wait, since the alternative is driving a page the extension
            # will never reach.
            await asyncio.sleep(0.25)
        if candidates:
            return candidates[-1]
        logger.warning(
            "[EXT] No TikTok tab in Dedicated Chrome after %.0fs; attaching to "
            "a blank page - the extension cannot inject there",
            self.TAB_WAIT_SECONDS,
        )
        context = self._browser.contexts[0]
        return context, await context.new_page()

    async def close(self):
        # Never close the remote Browser/Context: they belong to the Dedicated
        # Chrome service. Stopping Playwright only releases this agent's CDP
        # transport and leaves Chrome, its tabs and profile running.
        await self._disconnect()
        return {"success": True}

    async def _disconnect(self):
        playwright = self._playwright
        self._browser = self._context = self._page = self._playwright = None
        self._proxy_auth_session = None
        self._fedcm_session = None
        self._fedcm_dialog = None
        self._fedcm_dialog_shown_at = 0.0
        self._google_flow_active = False
        if playwright:
            await playwright.stop()

    async def open_tiktok(self):
        await self._ensure_started()
        if not self._is_tiktok_url(self._page.url):
            raise RuntimeError(
                "No TikTok tab is open in Dedicated Chrome; open it manually "
                "because URL navigation is disabled"
            )
        result = await self.click_first((
            '[data-e2e="nav-home"]',
            'a[href="/foryou"]',
            'a[href="/"]',
            'a:has-text("For You")',
            'a:has-text("Dành cho bạn")',
        ))
        await self._wait_for_ui_ready()
        return {
            **result,
            "success": True,
            "url": self._page.url,
            "expected_screen": "feed",
        }

    async def check_auth(self):
        """Return a clear auth state without submitting any login UI."""
        try:
            await self._ensure_started()
            snapshot = await self.snapshot()
            state = detect_auth_state(snapshot)
        except Exception:
            # "Temporary" is a claim about TikTok, and it used to be made about
            # anything at all - including a bug in this file, which then looked
            # exactly like a flaky page and stopped startup with no cause named.
            logger.exception("[AUTH] the authentication snapshot itself failed")
            return {
                "success": False,
                "auth_state": AuthState.TEMPORARY_ERROR.value,
                "url": None,
                "signals": {},
            }
        return {
            "success": state == AuthState.LOGGED_IN,
            "auth_state": state.value,
            "url": snapshot["url"],
            "signals": {
                "session_cookie_names": snapshot["session_cookie_names"],
                "authenticated_dom": snapshot["authenticated_dom"],
                "login_form_visible": snapshot["login_form_visible"],
            },
        }

    async def auto_login(self):
        """Recover the reviewed TikTok -> Google login flow with bounded retries.

        TikTok's Log in and Continue with Google controls are page DOM. Chrome's
        account chooser is FedCM browser UI, so the first account is selected
        through CDP. Password, CAPTCHA, OTP and device approval remain human-only.
        """
        auth = await self.check_auth()
        current = AuthState(auth.get("auth_state", AuthState.UNKNOWN.value))
        if current == AuthState.LOGGED_IN:
            self._google_flow_active = False
            return {**auth, "attempted": False, "reason": "already_logged_in"}
        if current in {AuthState.AUTH_RATE_LIMITED, AuthState.SECURITY_CHALLENGE}:
            return {**auth, "attempted": False, "reason": "human_verification_required"}
        if not self.auto_login_enabled:
            return {**auth, "attempted": False, "reason": "disabled"}

        now = monotonic()
        # FedCM is the continuation of a Google button click. Handle it before
        # the outer cooldown so one login flow is not paused for five minutes
        # between opening Google and choosing the account.
        fedcm = await self._select_first_fedcm_account(now, auth)
        if fedcm is not None:
            return fedcm
        if self._auto_login_attempts >= self.auto_login_max_attempts:
            return {**auth, "attempted": False, "reason": "attempt_limit_reached"}
        if now < self._auto_login_next_at:
            return {
                **auth,
                "attempted": False,
                "reason": "cooldown",
                "retry_after_seconds": round(self._auto_login_next_at - now, 1),
            }

        if self._google_auth_window_open():
            return {**auth, "attempted": False, "reason": "google_window_open"}

        google = await self._first_visible_locator((
            'button:has-text("Continue with Google")',
            '[role="button"]:has-text("Continue with Google")',
            # Confirmed live: TikTok's actual login-modal markup renders this
            # option as a `<div role="link">`, not a button/role=button at
            # all - without this, the two selectors above never match
            # anything and auto-login stops right here even with the modal
            # correctly open and the option visibly on screen.
            '[role="link"]:has-text("Continue with Google")',
            'button:has-text("Tiếp tục với Google")',
            '[role="button"]:has-text("Tiếp tục với Google")',
            '[role="link"]:has-text("Tiếp tục với Google")',
            'button:has-text("Log in with Google")',
            '[role="link"]:has-text("Log in with Google")',
            '[data-e2e*="google" i]',
        ))
        if google is None:
            login = await self._first_visible_locator((
                '[data-e2e="top-login-button"]',
                # :has-text (substring), not :text-is (exact) - confirmed
                # live that TikTok's real, visible "Log in" buttons carry
                # extra nested markup (an icon span), so their exact text
                # content is not literally "Log in" even though that is all
                # that renders; :text-is only matched a hidden, markup-free
                # duplicate and never the real button.
                'button:has-text("Log in")', 'a:has-text("Log in")',
                'button:has-text("Đăng nhập")', 'a:has-text("Đăng nhập")',
            ))
            if login is None:
                return {**auth, "attempted": False, "reason": "login_entry_not_visible"}
            logger.info("[AUTH] opening TikTok login modal")
            await self._click_dom_target_center(login, "tiktok_login")
            await self._page.wait_for_timeout(1000)
            google = await self._first_visible_locator((
                'button:has-text("Continue with Google")',
                '[role="button"]:has-text("Continue with Google")',
                'button:has-text("Tiếp tục với Google")',
                '[role="button"]:has-text("Tiếp tục với Google")',
                '[data-e2e*="google" i]',
            ))
            if google is None:
                return {**auth, "attempted": False, "reason": "login_modal_opened_google_missing"}

        # Count before interacting: a navigation/CDP failure after submit must
        # still consume the attempt, otherwise a crash loop could resubmit.
        self._auto_login_attempts += 1
        self._auto_login_next_at = now + self.auto_login_cooldown_seconds
        self._google_flow_active = True
        logger.info(
            "[AUTH] clicking Continue with Google attempt=%s/%s",
            self._auto_login_attempts, self.auto_login_max_attempts,
        )
        await self._click_dom_target_center(google, "continue_with_google")
        if self.auto_login_post_submit_wait_seconds:
            await self._page.wait_for_timeout(
                int(self.auto_login_post_submit_wait_seconds * 1000)
            )
        result = await self.check_auth()
        return {
            **result,
            "attempted": True,
            "reason": (
                "login_confirmed"
                if result.get("auth_state") == AuthState.LOGGED_IN.value
                else "google_continue_clicked"
            ),
            "attempts": self._auto_login_attempts,
        }

    async def _first_visible_locator(self, selectors):
        """The first genuinely visible match, across every selector AND
        every element each selector matches - not just each selector's
        `.first`.

        Confirmed live against a real TikTok page: `data-e2e="top-login-
        button"` currently sits on a zero-size, detached duplicate that
        happens to come before the two real, visible "Log in" buttons in
        DOM order. `.first` alone picks that hidden duplicate, reports it
        as not visible, and gives up on the selector entirely instead of
        checking the real button right after it - auto-login then never
        finds a way in, even though a visible "Log in" button is on screen.
        """
        for selector in selectors:
            locator = self._page.locator(selector)
            count = await locator.count()
            for index in range(count):
                candidate = locator.nth(index)
                if await candidate.is_visible():
                    return candidate
        return None

    async def _click_dom_target_center(self, locator, target):
        """Read fresh DOM geometry and click its center in CSS viewport pixels."""
        if not await locator.is_visible():
            raise RuntimeError(f"DOM target became hidden before click: {target}")
        box = await locator.bounding_box()
        if not box or box.get("width", 0) <= 0 or box.get("height", 0) <= 0:
            raise RuntimeError(f"DOM target has no clickable geometry: {target}")
        x = float(box["x"]) + float(box["width"]) / 2
        y = float(box["y"]) + float(box["height"]) / 2
        viewport = await self._page.evaluate(
            "() => ({width: innerWidth, height: innerHeight})"
        )
        if not (0 <= x < float(viewport["width"]) and 0 <= y < float(viewport["height"])):
            raise RuntimeError(f"DOM target center is outside viewport: {target}")
        logger.info(
            "[AUTH] DOM target=%s center=(%.1f,%.1f) size=(%.1f,%.1f)",
            target, x, y, float(box["width"]), float(box["height"]),
        )
        await self._page.mouse.click(x, y)
        return {"target": target, "x": x, "y": y, "box": dict(box)}

    def _google_auth_window_open(self):
        for page in getattr(self._context, "pages", ()):
            if page is self._page or page.is_closed():
                continue
            hostname = (urlparse(page.url).hostname or "").casefold()
            if hostname == "accounts.google.com" or hostname.endswith(".accounts.google.com"):
                return True
        return False

    @property
    def egw_enabled(self):
        return bool(self.egw_workspace_id and self.egw_token)

    async def _install_proxy_auth(self):
        """Answer the egress proxy's 407 through CDP so Chrome never shows
        its own credential dialog.

        Order matters and is not negotiable: `Fetch.enable` without a filter
        pauses EVERY request on this page until something calls
        `continueRequest`. Registering the handlers after enabling would leave
        a window where requests pause with nobody to release them; a missing
        `requestPaused` handler would hang the page outright. So: handlers
        first, then enable. Credentials go only to a challenge whose source is
        the proxy - TikTok's own 401s must keep their default handling.
        """
        session = await self._context.new_cdp_session(self._page)

        def _spawn(coro):
            task = asyncio.ensure_future(coro)
            task.add_done_callback(self._log_proxy_auth_task_error)

        async def on_auth_required(event):
            challenge = event.get("authChallenge") or {}
            if challenge.get("source") == "Proxy":
                response = {
                    "response": "ProvideCredentials",
                    "username": self.egw_workspace_id,
                    "password": self.egw_token,
                }
            else:
                response = {"response": "Default"}
            await session.send("Fetch.continueWithAuth", {
                "requestId": event["requestId"],
                "authChallengeResponse": response,
            })

        async def on_request_paused(event):
            await session.send("Fetch.continueRequest", {"requestId": event["requestId"]})

        session.on("Fetch.authRequired", lambda e: _spawn(on_auth_required(e)))
        session.on("Fetch.requestPaused", lambda e: _spawn(on_request_paused(e)))
        await session.send("Fetch.enable", {
            "handleAuthRequests": True,
            "patterns": [{"urlPattern": "*"}],
        })
        self._proxy_auth_session = session
        logger.info("[EGW] proxy auth handler installed workspace=%s", self.egw_workspace_id)

    @staticmethod
    def _log_proxy_auth_task_error(task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            # A single lost continue* call stalls one request, not the page;
            # log it (without the token) so a pattern is visible in the logs.
            logger.warning("[EGW] proxy auth callback failed error_type=%s", type(exc).__name__)

    async def verify_egress_ip(self, expected_ip=""):
        """Prove the tab's traffic really leaves through the workspace's IP.

        Uses `fetch()` from inside the TikTok page: same network stack, same
        proxy and credentials as every request the agent will make - unlike
        Playwright's `request` context, which for a CDP-attached browser goes
        straight out of the host and would happily report the wrong IP. No
        URL navigation involved.

        With `expected_ip` the check is exact. Without it (token pasted by
        hand, route unknown here) the observed IP must at least differ from
        the host's own direct egress IP: identical means the proxy is not in
        the path at all. Fail-closed: any doubt is "not verified".
        """
        await self._ensure_started()
        probe = (
            "async () => { const r = await fetch('https://api.ipify.org?format=text', "
            "{cache: 'no-store'}); if (!r.ok) throw new Error('probe status ' + r.status); "
            "return (await r.text()).trim(); }"
        )
        try:
            observed = str(await self._page.evaluate(probe)).strip()
        except Exception as exc:
            logger.error(
                "[EGW] egress probe failed error_type=%s", type(exc).__name__, exc_info=True,
            )
            return {"verified": False, "observed_ip": None, "reason": "probe_failed"}

        if expected_ip:
            ok = observed == str(expected_ip).strip()
            reason = None if ok else "observed_ip_differs_from_expected"
            level = logging.INFO if ok else logging.ERROR
            logger.log(level, "[EGW] egress observed=%s expected=%s verified=%s",
                       observed, expected_ip, ok)
            return {"verified": ok, "observed_ip": observed, "reason": reason}

        host_ip = await self._direct_host_ip()
        if host_ip is None:
            logger.warning("[EGW] egress observed=%s; host IP unknown, cannot prove proxy is in path", observed)
            return {"verified": False, "observed_ip": observed, "reason": "host_ip_unknown"}
        ok = observed != host_ip
        reason = None if ok else "observed_ip_equals_host_ip"
        logger.log(logging.INFO if ok else logging.ERROR,
                   "[EGW] egress observed=%s host=%s verified=%s", observed, host_ip, ok)
        return {"verified": ok, "observed_ip": observed, "reason": reason}

    @staticmethod
    async def _direct_host_ip():
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                response = await client.get("https://api.ipify.org?format=text")
                response.raise_for_status()
                return response.text.strip() or None
        except Exception as exc:
            logger.warning(
                "[EGW] direct host IP probe failed error_type=%s", type(exc).__name__,
                exc_info=True,
            )
            return None

    async def _install_fedcm_bridge(self):
        """Observe Chrome-owned FedCM UI; unsupported Chrome versions fall back."""
        try:
            session = await self._context.new_cdp_session(self._page)
            session.on("FedCm.dialogShown", self._on_fedcm_dialog_shown)
            session.on("FedCm.dialogClosed", self._on_fedcm_dialog_closed)
            await session.send("FedCm.enable", {"disableRejectionDelay": False})
            self._fedcm_session = session
        except Exception as exc:  # noqa: BLE001 - older Chrome has no FedCM CDP domain
            if not self._fedcm_unavailable_logged:
                logger.info(
                    "[AUTH] FedCM CDP unavailable; Google account selection stays manual "
                    "error_type=%s", type(exc).__name__,
                )
                self._fedcm_unavailable_logged = True

    def _on_fedcm_dialog_shown(self, event):
        self._fedcm_dialog = dict(event or {})
        self._fedcm_dialog_shown_at = monotonic()
        logger.info(
            "[AUTH] FedCM dialog observed type=%s accounts=%s",
            self._fedcm_dialog.get("dialogType", "UNKNOWN"),
            len(self._fedcm_dialog.get("accounts") or []),
        )

    def _on_fedcm_dialog_closed(self, event):
        dialog_id = str((event or {}).get("dialogId") or "")
        if not self._fedcm_dialog or dialog_id == str(self._fedcm_dialog.get("dialogId") or ""):
            self._fedcm_dialog = None
            self._fedcm_dialog_shown_at = 0.0
            self._google_flow_active = False

    async def _select_first_fedcm_account(self, now, auth):
        dialog = self._fedcm_dialog
        if dialog is None or self._fedcm_session is None:
            return None
        age = now - self._fedcm_dialog_shown_at
        if age < self.auto_login_fedcm_settle_seconds:
            return {
                **auth,
                "attempted": False,
                "reason": "fedcm_settling",
                "retry_after_seconds": round(self.auto_login_fedcm_settle_seconds - age, 1),
            }

        dialog_id = str(dialog.get("dialogId") or "")
        dialog_type = str(dialog.get("dialogType") or "")
        accounts = dialog.get("accounts") or []
        if not dialog_id:
            return {**auth, "attempted": False, "reason": "fedcm_dialog_missing_id"}
        if dialog_type in {"AccountChooser", "AutoReauthn"}:
            if not accounts:
                return {**auth, "attempted": False, "reason": "fedcm_has_no_accounts"}
            command = "FedCm.selectAccount"
            parameters = {"dialogId": dialog_id, "accountIndex": 0}
        elif dialog_type == "ConfirmIdpLogin":
            command = "FedCm.clickDialogButton"
            parameters = {
                "dialogId": dialog_id,
                "dialogButton": "ConfirmIdpLoginContinue",
            }
        else:
            return {**auth, "attempted": False, "reason": "fedcm_requires_human"}

        if not self._google_flow_active:
            if self._auto_login_attempts >= self.auto_login_max_attempts:
                return {**auth, "attempted": False, "reason": "attempt_limit_reached"}
            if now < self._auto_login_next_at:
                return {
                    **auth,
                    "attempted": False,
                    "reason": "cooldown",
                    "retry_after_seconds": round(self._auto_login_next_at - now, 1),
                }
            self._auto_login_attempts += 1
            self._auto_login_next_at = now + self.auto_login_cooldown_seconds
        logger.info(
            "[AUTH] selecting first FedCM account attempt=%s/%s",
            self._auto_login_attempts, self.auto_login_max_attempts,
        )
        await self._fedcm_session.send(command, parameters)
        self._fedcm_dialog = None
        self._google_flow_active = False
        if self.auto_login_post_submit_wait_seconds:
            await self._page.wait_for_timeout(
                int(self.auto_login_post_submit_wait_seconds * 1000)
            )
        result = await self.check_auth()
        return {
            **result,
            "attempted": True,
            "reason": (
                "login_confirmed"
                if result.get("auth_state") == AuthState.LOGGED_IN.value
                else "fedcm_first_account_selected"
            ),
            "attempts": self._auto_login_attempts,
        }

    async def open_url(self, url):
        self._validate_url(url)
        raise RuntimeError(
            "Direct URL navigation is disabled; use a reviewed TikTok UI action"
        )

    async def reload_page(self):
        """Reload the current tab - the one exception to "no navigation".

        `open_url` above stays blocked (the model must never choose to
        navigate); this is a same-URL reload, never model-selectable (see
        `core/policy.py`'s `MESSAGE_SCAN_CAPABILITIES` gate), and only ever
        triggered by `InboxScanSkill._maybe_reload_for_new_activity` off a
        live signal, never on a blind timer. It exists because TikTok's own
        message-request queue does not refresh on its own once the page has
        been sitting loaded for a while - confirmed live: a stranger's first
        message never appeared in `list_message_requests` until the page was
        reloaded, even though the Activity/notification badge that triggers
        this had already ticked up.
        """
        await self._ensure_started()
        await self._page.reload(wait_until="domcontentloaded")
        await self._wait_for_ui_ready()
        return {"success": True, "url": self._page.url}

    async def back(self):
        await self._ensure_started()
        result = await self.click_first((
            '[data-e2e="chat-back"]',
            '[data-e2e="browse-back"]',
            'button[aria-label="Back"]',
            'button[aria-label="Quay lại"]',
        ))
        await self._wait_for_ui_ready()
        return {**result, "success": True, "url": self._page.url}

    async def screenshot(self):
        await self._ensure_started()
        async with self._screenshot_lock:
            if self._screenshot_attempt_count >= self.max_screenshots_per_run:
                raise RuntimeError(
                    "Screenshot limit reached for this agent task"
                )
            if self.max_screenshots_per_run == 0:
                raise RuntimeError("Screenshots are disabled for this agent run")

            started_at = self._run_started_at or monotonic()
            remaining = self.screenshot_delay_seconds - (monotonic() - started_at)
            if remaining > 0:
                await asyncio.sleep(remaining)

            # Loading/blank pages are rejected before consuming a capture
            # attempt; Vision never receives a known bad screenshot.
            await self._wait_for_ui_ready()
            self._screenshot_attempt_count += 1
            self.screenshot_dir.mkdir(parents=True, exist_ok=True)
            path = self.screenshot_dir / f"chrome-{time_ns()}.png"
            await self._page.screenshot(path=str(path), full_page=False)
            return str(path)

    async def click_coordinate(
        self, x, y, target=None, expected_screen=None, observed_viewport=None
    ):
        """Click a coordinate already validated by the coordinate resolver."""
        await self._ensure_started()
        layout = await self._page.evaluate(
            """() => {
                const visible = node => {
                    const rect = node.getBoundingClientRect();
                    const style = getComputedStyle(node);
                    return rect.width > 0 && rect.height > 0
                        && style.display !== 'none' && style.visibility !== 'hidden';
                };
                const overlays = [...document.querySelectorAll(
                    '[role="dialog"],[aria-modal="true"],'
                    + '[data-e2e*="modal" i],[data-e2e*="popup" i]'
                )];
                return {
                    width: window.innerWidth,
                    height: window.innerHeight,
                    ready: document.readyState === 'interactive'
                        || document.readyState === 'complete',
                    blockingOverlay: overlays.some(visible),
                };
            }"""
        )
        viewport = {"width": layout["width"], "height": layout["height"]}
        if observed_viewport and tuple(observed_viewport) != (
            viewport["width"], viewport["height"]
        ):
            raise RuntimeError("Viewport changed after fixed-coordinate observation")
        if not layout["ready"] or layout["blockingOverlay"]:
            raise RuntimeError("UI is not safe for a fixed-coordinate click")
        if not isinstance(x, int) or not isinstance(y, int):
            raise ValueError("Fixed coordinates must be integer CSS viewport pixels")
        if not (0 <= x < viewport["width"] and 0 <= y < viewport["height"]):
            raise RuntimeError("Fixed coordinate is outside the current viewport")
        previous_sequence = await self.current_extension_sequence()
        action_started_at = monotonic()
        await self._page.mouse.click(x, y)
        return {
            "success": True,
            "source": "fixed_coordinate",
            "target": target,
            "coordinate": (x, y),
            "predicted_point": (x, y),
            "clicked_point": (x, y),
            "previous_extension_sequence": previous_sequence,
            "expected_screen": expected_screen,
            "action_started_at": action_started_at,
        }

    async def snapshot(self):
        await self._ensure_started()
        extension_state = await self._read_extension_state()
        extension_rejected = False
        if self._force_dom_snapshot_once:
            self._force_dom_snapshot_once = False
            extension_state = None
            extension_rejected = True
        elif extension_state is not None and int(extension_state.get("sequence", 0)) <= (
            self._extension_state_floor
        ):
            extension_state = None
            extension_rejected = True
        if (
            extension_state is None
            and not extension_rejected
            and not self._extension_unavailable_logged
        ):
            logger.warning("[EXT] Unavailable - fallback to browser DOM")
            self._extension_unavailable_logged = True
        title = await self._page.title()
        body_text = await self._page.locator("body").inner_text(timeout=self.timeout_ms)
        elements = await self._page.locator(
            "a,button,input,textarea,[role=button],[role=link],[role=textbox],"
            '[data-e2e="dm-new-chatbox"],[data-e2e="dm-new-chat-nickname"],'
            '[data-e2e="chat-uniqueid"],[data-e2e="dm-new-message-text"],'
            '[data-e2e="dm-new-conversation-item"],[data-e2e="dm-new-input-editor"],'
            '[data-e2e="message-input-area"]'
        ).evaluate_all(
            """nodes => nodes.slice(0, 500).map((node, index) => ({
                index,
                text: (
                    node.tagName === 'TEXTAREA' ? (node.value || '') : (node.innerText || '')
                ).trim().slice(0, 500),
                description: (node.getAttribute('aria-label') || node.getAttribute('title') || '').trim(),
                resource_id: node.getAttribute('data-e2e') || node.id || '',
                role: node.getAttribute('role') || node.tagName.toLowerCase(),
                href: node.href || '',
                enabled: !node.disabled,
                visible: !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length),
                bbox: (() => {
                    const rect = node.getBoundingClientRect();
                    return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
                })()
            }))"""
        )
        # Armed by opening an explicitly unread row, consumed here. Only the
        # vision path still reads it: it tells `_persist_visual_conversation`
        # which conversation a screenshot belongs to.
        #
        # A second message parser used to live here - a page.evaluate that
        # reverse-engineered inbound messages from `[data-conversation-id]` and
        # `data-direction`. TikTok stopped rendering both, so it had been
        # returning nothing for a long time (`published_events=0` in every
        # scan) while looking like a working ingestion path. It also duplicated
        # what the extension already parses correctly, which is how the two
        # drifted apart. The extension is now the only place that reads a
        # message out of the DOM.
        # Cookie values are held only in this local expression and are never
        # returned, persisted or logged.
        cookie_names = {cookie["name"] for cookie in await self._context.cookies([self.base_url])}
        session_cookie_names = sorted(
            cookie_names.intersection({"sessionid", "sessionid_ss", "sid_tt", "uid_tt"})
        )
        authenticated_dom = await self._has_visible_authenticated_dom()
        login_form_visible = await self._has_visible_login_form()
        render_state = await self._render_state()
        layout_state = await self._page.evaluate(
            """() => {
                const visible = node => {
                    const rect = node.getBoundingClientRect();
                    const style = getComputedStyle(node);
                    return rect.width > 0 && rect.height > 0
                        && style.display !== 'none' && style.visibility !== 'hidden';
                };
                const overlays = [...document.querySelectorAll(
                    '[role="dialog"],[aria-modal="true"],'
                    + '[data-e2e*="modal" i],[data-e2e*="popup" i]'
                )];
                return {
                    viewport: {width: window.innerWidth, height: window.innerHeight},
                    blocking_overlay_present: overlays.some(visible),
                };
            }"""
        )
        if extension_state is not None:
            # The extension's semantic roles come first so role-based lookups
            # win, followed by the raw Playwright elements, which still carry
            # the authentication evidence the extension does not own.
            merged = list(extension_state["elements"]) + list(elements)
            elements = [
                {**element, "index": index} for index, element in enumerate(merged)
            ]
        compact = json.dumps(
            {"url": self._page.url, "title": title, "elements": elements},
            ensure_ascii=False, sort_keys=True,
        )
        return {
            "url": self._page.url,
            "title": title,
            "body_text": body_text[:50000],
            "elements": elements,
            "signature": hashlib.sha256(compact.encode()).hexdigest(),
            "session_cookie_names": session_cookie_names,
            "authenticated_dom": authenticated_dom,
            "login_form_visible": login_form_visible,
            "page_render_ready": render_state["ready"],
            "page_loading": render_state["loading"],
            "viewport": layout_state["viewport"],
            "blocking_overlay_present": layout_state["blocking_overlay_present"],
            "ui_abnormal": not render_state["ready"] or render_state["loading"],
            "last_send_evidence": self._last_send_evidence,
            "extension_state": extension_state,
        }

    async def current_extension_sequence(self):
        """Return the latest valid Extension sequence visible before an action."""
        await self._ensure_started()
        state = await self._read_extension_state()
        if state:
            return int(state.get("sequence", 0) or 0)
        return int(self._last_extension_sequence or 0)

    async def wait_for_perception_event(
        self, timeout_ms=1200, previous_sequence=None, expected_screen=None
    ):
        """Wait briefly for a newer read-only extension event after an action.

        Timeout and missing-extension cases are normal: callers immediately
        fall back to the regular DOM snapshot path.
        """
        await self._ensure_started()
        if self._perception_wait_result is not None:
            result = self._perception_wait_result
            self._perception_wait_result = None
            return result
        bridge = self._page.locator("#autonomous-agent-perception-v1")
        if await bridge.count() == 0:
            return False
        previous = (
            self._last_extension_sequence
            if previous_sequence is None else int(previous_sequence or 0)
        )
        try:
            await self._page.wait_for_function(
                """({bridgeId, previous, expectedScreen}) => {
                    const node = document.getElementById(bridgeId);
                    if (Number(node?.dataset?.sequence || 0) <= previous) return false;
                    if (!expectedScreen) return true;
                    try {
                        const payload = JSON.parse(node.textContent || '{}');
                        return String(payload?.ui_state || '').toUpperCase() === expectedScreen;
                    } catch (_) {
                        return false;
                    }
                }""",
                {
                    "bridgeId": "autonomous-agent-perception-v1",
                    "previous": previous,
                    "expectedScreen": normalize_ui_state(expected_screen),
                },
                timeout=max(0, int(timeout_ms)),
            )
            return True
        except Exception:  # noqa: BLE001 - a wait that did not complete is a no
            return False

    BRIDGE_SELECTOR = "#autonomous-agent-perception-v1"

    def _reject_extension_state(self, reason, **details):
        """Log exactly why a payload was dropped and fall back to browser DOM."""
        suffix = " ".join(f"{key}={value!r}" for key, value in details.items())
        logger.warning("[EXT] rejected reason=%s%s", reason, f" {suffix}" if suffix else "")
        return None

    @staticmethod
    def _normalized_rect(value):
        """Coerce a rect to numbers. A missing rect never drops the element."""
        rect = value if isinstance(value, dict) else {}
        out = {}
        for key in ("x", "y", "width", "height"):
            try:
                out[key] = float(rect.get(key))
            except (TypeError, ValueError):
                out[key] = None
        return out

    @classmethod
    def _normalize_extension_element(cls, index, element):
        """Schema v2 element -> flat snapshot element.

        `selector` stays a CSS selector and is never re-labelled as an Android
        `resource_id`; `resource_id` carries the platform's own stable id
        (data-e2e / element id) that the DOM layers match on.
        """
        rect = cls._normalized_rect(element.get("rect"))
        return {
            "index": index,
            "id": str(element.get("id", ""))[:200],
            "text": str(element.get("text", ""))[:500],
            "description": str(element.get("description", ""))[:500],
            "resource_id": str(element.get("resource_id", ""))[:200],
            "selector": str(element.get("selector", ""))[:500],
            "role": str(element.get("role", ""))[:100],
            "href": str(element.get("href", ""))[:1000],
            "enabled": bool(element.get("enabled", True)),
            "visible": bool(element.get("visible", True)),
            "unread": (
                None if element.get("unread") is None else bool(element.get("unread"))
            ),
            "rect": rect,
            # Coordinate consumers read `bbox`; it is the same rect, not a
            # second source of truth.
            "bbox": dict(rect),
        }

    @staticmethod
    def _normalize_extension_conversation(index, item):
        username = str(item.get("username") or item.get("text") or "").strip()
        return {
            "id": str(item.get("id") or f"conversation_{index + 1}")[:200],
            "key": str(item.get("key") or username)[:200],
            "username": username[:400],
            "text": username[:400],
            "preview": str(item.get("preview") or "").strip()[:400],
            "unread": bool(item.get("unread")),
            "message_request": bool(item.get("message_request")),
            "visible": bool(item.get("visible", True)),
            "selector": str(item.get("selector") or "")[:500],
            "rect": ChromeBrowserTool._normalized_rect(item.get("rect")),
        }

    async def _read_extension_state(self):
        """Read and normalize the extension's schema v2 DOM bridge.

        Rejections are always logged with a reason; this method never swallows
        an error silently. `None` means "use the plain browser DOM instead".
        """
        raw = None
        # The bridge read gets a second chance only when this run has already
        # seen the extension publish: the payload is then known to exist and a
        # miss means the page was busy (staging an attachment, a heavy
        # re-render), not that no extension is installed. Without this, a
        # single missed read fell back to the raw browser DOM, whose state
        # carries no header username at all - which the recipient gate read as
        # "the wrong thread is open" and aborted a correct reply mid-flow
        # (ws-bc391da5, 2026-09-21). An account with no extension still pays
        # only one timeout per observation.
        attempts = 2 if self._extension_connected else 1
        for attempt in range(1, attempts + 1):
            try:
                raw = await self._page.locator(
                    self.BRIDGE_SELECTOR
                ).text_content(timeout=500)
                break
            except Exception as exc:  # noqa: BLE001 - retried; the last attempt is logged
                if attempt < attempts:
                    logger.info(
                        "[EXT] bridge read missed its budget attempt=%s/%s "
                        "error_type=%s; reading again",
                        attempt, attempts, type(exc).__name__,
                    )
                    continue
                if not self._extension_unavailable_logged:
                    logger.warning(
                        "[EXT] rejected reason=bridge_unreadable attempts=%s "
                        "error=%s: %s",
                        attempts, type(exc).__name__, exc,
                    )
                    self._extension_unavailable_logged = True
                return None

        if not raw:
            return self._reject_extension_state("empty_payload")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            return self._reject_extension_state("invalid_json", error=str(exc))
        if not isinstance(payload, dict):
            return self._reject_extension_state(
                "invalid_schema", payload_type=type(payload).__name__
            )
        if payload.get("schema_version") != 2:
            return self._reject_extension_state(
                "invalid_schema", schema_version=payload.get("schema_version")
            )
        if payload.get("platform") != "tiktok_web":
            return self._reject_extension_state(
                "invalid_platform", platform=payload.get("platform")
            )

        elements = payload.get("elements")
        if not isinstance(elements, list):
            return self._reject_extension_state(
                "invalid_elements_type", elements_type=type(elements).__name__
            )
        try:
            sequence = int(payload.get("sequence", 0))
        except (TypeError, ValueError):
            return self._reject_extension_state(
                "invalid_sequence", sequence=payload.get("sequence")
            )
        if sequence <= 0:
            return self._reject_extension_state("invalid_sequence", sequence=sequence)

        ui_state = str(payload.get("ui_state", "")).strip().upper()
        if ui_state not in UI_STATES:
            return self._reject_extension_state("unknown_ui_state", ui_state=ui_state)

        try:
            confidence = float(payload.get("page_confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        normalized_elements = [
            self._normalize_extension_element(index, element)
            for index, element in enumerate(elements[:500])
            if isinstance(element, dict)
        ]

        payload["ui_state"] = ui_state
        payload["page_confidence"] = confidence
        payload["elements"] = normalized_elements
        payload["messages_mode"] = (
            str(payload.get("messages_mode")).strip().lower()
            if payload.get("messages_mode") else None
        )
        payload["conversations"] = [
            self._normalize_extension_conversation(index, item)
            for index, item in enumerate(list(payload.get("conversations") or [])[:100])
            if isinstance(item, dict)
        ]
        payload["messages"] = [
            item for item in list(payload.get("messages") or [])[-200:]
            if isinstance(item, dict)
        ]
        for key in ("modal", "composer", "send_button", "messages_panel", "active_chat"):
            payload[key] = payload[key] if isinstance(payload.get(key), dict) else {}
        payload["events"] = [
            str(event).strip().upper()
            for event in (payload.get("events") or []) if event
        ][:20]
        for key in ("messages_unread_count", "activity_unread_count", "unread_count"):
            try:
                payload[key] = max(0, int(payload.get(key) or 0))
            except (TypeError, ValueError):
                payload[key] = 0

        self._last_extension_sequence = max(self._last_extension_sequence, sequence)
        self._extension_unavailable_logged = False
        self._trace_extension_payload(payload)
        logger.info(
            "[EXT] Available state=%s seq=%s elements=%s",
            ui_state, sequence, len(normalized_elements),
        )
        return payload

    async def _install_extension_event_bridge(self):
        """Forward read-only extension DOM events into Python logging/state."""
        binding_name = f"__autonomousAgentPerception_{id(self):x}"

        async def receive(_source, payload):
            if not isinstance(payload, dict):
                return
            self._latest_extension_payload = payload
            try:
                self._trace_extension_payload(payload)
            except (TypeError, ValueError, AttributeError):
                logger.debug("Extension event rejected by Python trace validator")

        await self._context.expose_binding(binding_name, receive)
        script = """({bindingName}) => {
            if (globalThis.__autonomousAgentPerceptionBridgeInstalled) return;
            globalThis.__autonomousAgentPerceptionBridgeInstalled = true;
            let lastSequence = 0;
            const forward = () => {
                const node = document.getElementById('autonomous-agent-perception-v1');
                const sequence = Number(node?.dataset?.sequence || 0);
                if (!node || sequence <= lastSequence) return;
                try {
                    const payload = JSON.parse(node.textContent || '');
                    lastSequence = sequence;
                    Promise.resolve(globalThis[bindingName](payload)).catch(() => {});
                } catch (_) {}
            };
            const start = () => {
                new MutationObserver(forward).observe(document.documentElement, {
                    subtree: true, childList: true, characterData: true, attributes: true
                });
                forward();
            };
            if (document.documentElement) start();
            else document.addEventListener('DOMContentLoaded', start, {once: true});
        }"""
        await self._context.add_init_script(
            f"({script})({{bindingName: {json.dumps(binding_name)}}})"
        )
        await self._page.evaluate(script, {"bindingName": binding_name})

    def _trace_extension_payload(self, payload):
        """Log extension lifecycle/state transitions without message content."""
        sequence = int(payload.get("sequence", 0) or 0)
        if sequence <= 0:
            return
        ui_state = str(payload.get("ui_state", "UNKNOWN")).strip().upper()
        confidence = max(0.0, min(1.0, float(payload.get("page_confidence", 0.0) or 0.0)))
        mode = payload.get("messages_mode") or "none"
        url = str(payload.get("url", ""))[:500]
        if not self._extension_connected:
            logger.info("[EXT] Connected")
            self._extension_connected = True
        self._extension_unavailable_logged = False
        if url and url != self._extension_url:
            logger.info("[EXT] Content script active url=%s", url)
            self._extension_url = url

        conversations = payload.get("conversations") or []
        composer = payload.get("composer") or {}
        send_button = payload.get("send_button") or {}
        state_key = (
            ui_state,
            mode,
            bool(composer.get("present")),
            bool(send_button.get("present")),
            bool((payload.get("active_chat") or {}).get("present")),
            int(payload.get("messages_unread_count", 0) or 0),
            tuple(
                (item.get("id"), bool(item.get("unread")))
                for item in conversations[:100] if isinstance(item, dict)
            ),
            tuple(
                item.get("id") for item in (payload.get("messages") or [])[-50:]
                if isinstance(item, dict)
            ),
            (payload.get("security_challenge") or {}).get("type"),
        )
        if self._extension_screen is None or ui_state != self._extension_screen:
            if self._extension_screen is not None:
                logger.info("[EXT] Screen changed=%s -> %s", self._extension_screen, ui_state)
            logger.info(
                "[EXT] state=%s mode=%s seq=%s confidence=%.2f conversations=%s composer=%s",
                ui_state, mode, sequence, confidence, len(conversations),
                bool(composer.get("present")),
            )
        elif state_key != self._extension_state_key:
            logger.info(
                "[EXT] State changed state=%s mode=%s seq=%s conversations=%s composer=%s",
                ui_state, mode, sequence, len(conversations), bool(composer.get("present")),
            )
        self._extension_screen = ui_state
        self._extension_state_key = state_key

    async def set_visual_targets(self, targets, screenshot_path, confidence_threshold=0.85):
        """Replace the one-observation registry used by click_bbox."""
        self._visual_targets = {target.target_id: target for target in targets}
        self._visual_screenshot = screenshot_path
        self._visual_url = self._page.url if self._page else None
        self._visual_target_threshold = float(confidence_threshold)

    async def click_bbox(self, target_id):
        """Click only a current, observer-issued visual target by opaque id."""
        await self._ensure_started()
        if not isinstance(target_id, str) or set(target_id) - set(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        ):
            raise ValueError("A valid observer-issued visual target_id is required")
        target = self._visual_targets.get(target_id)
        if target is None or not self._visual_screenshot:
            raise RuntimeError("Visual target is stale or was not issued by the current observation")
        if self._page.url != self._visual_url:
            raise RuntimeError("Page changed after the visual target observation")
        if target.confidence < self._visual_target_threshold:
            raise RuntimeError("Visual target confidence is below the configured threshold")
        if await self._dom_equivalent_visible(target):
            raise RuntimeError("A DOM equivalent is visible; visual coordinate click is not allowed")

        viewport = await self._page.evaluate(
            "() => ({width: window.innerWidth, height: window.innerHeight})"
        )
        box = target.bbox
        if (
            box.x < 0 or box.y < 0 or box.width <= 0 or box.height <= 0
            or box.x + box.width > viewport["width"]
            or box.y + box.height > viewport["height"]
        ):
            raise RuntimeError("Visual target bbox is outside the current viewport")
        x = box.x + box.width / 2
        y = box.y + box.height / 2
        previous_sequence = await self.current_extension_sequence()
        action_started_at = monotonic()
        await self._page.mouse.click(x, y)
        expected_screen = {
            "inbox_button": "messages",
            "conversation": "conversation",
            "conversation_item": "conversation",
            "unread_conversation": "conversation",
        }.get(target.type)
        self._visual_targets = {}
        self._visual_screenshot = None
        self._visual_url = None
        return {
            "success": True,
            "source": "gemini",
            "target_id": target_id,
            "target_type": target.type,
            "predicted_point": (x, y),
            "clicked_point": (x, y),
            "confidence": target.confidence,
            "previous_extension_sequence": previous_sequence,
            "expected_screen": expected_screen,
            "action_started_at": action_started_at,
        }

    async def _dom_equivalent_visible(self, target):
        selectors = {
            "inbox_button": (
                '[data-e2e="nav-message"],a[href="/messages"],'
                'a:has-text("Messages"),a:has-text("Tin nhắn")'
            ),
            "conversation": '[data-e2e="dm-new-conversation-item"]',
            "conversation_item": '[data-e2e="dm-new-conversation-item"]',
            "unread_conversation": '[data-e2e="dm-new-conversation-item"]',
            "message_input": (
                '[data-e2e="dm-new-input-editor"] [contenteditable="true"],'
                '[role="textbox"][aria-label*="Send a message" i]'
            ),
            "send_button": '[data-e2e*="send" i],button[aria-label*="send" i]',
            "back_button": 'button[aria-label*="back" i],[data-e2e*="back" i]',
            "popup_close": (
                'button[aria-label*="close" i],button[aria-label*="đóng" i],'
                '[data-e2e*="close" i]'
            ),
        }
        selector = selectors.get(target.type)
        if not selector:
            return False
        locator = self._page.locator(selector)
        for index in range(min(await locator.count(), 20)):
            candidate = locator.nth(index)
            if not await candidate.is_visible():
                continue
            if target.type != "conversation" or not target.text:
                return True
            exact = candidate.get_by_text(target.text, exact=True)
            for match_index in range(await exact.count()):
                if await exact.nth(match_index).is_visible():
                    return True
        return False

    async def click_ui_target(self, selectors, target_name, expected_screen=None):
        """Click a reviewed TikTok target and expose a pre-action state barrier."""
        await self._ensure_started()
        previous_sequence = await self.current_extension_sequence()
        extension_state = await self._read_extension_state()
        extension_elements = (extension_state or {}).get("elements") or []
        extension_has_target = False
        if target_name == "inbox_button":
            extension_has_target = any(
                item.get("visible", True) and (
                    str(item.get("resource_id", "")).casefold() in {
                        "nav-message", "top-dm-icon", "inbox-icon",
                    }
                    or "message" in str(item.get("resource_id", "")).casefold()
                    or str(item.get("text", "")).strip().casefold()
                    in {"messages", "inbox", "tin nhắn", "hộp thư"}
                    or str(item.get("description", "")).strip().casefold()
                    in {"messages", "inbox", "tin nhắn", "hộp thư"}
                )
                for item in extension_elements
            )

        for selector in selectors:
            locator = self._page.locator(selector)
            for index in range(min(await locator.count(), 5)):
                candidate = locator.nth(index)
                if not await candidate.is_visible():
                    continue
                box = await candidate.bounding_box()
                action_started_at = monotonic()
                await candidate.click()
                clicked_point = (
                    (round(box["x"] + box["width"] / 2),
                     round(box["y"] + box["height"] / 2))
                    if box else None
                )
                return {
                    "success": True,
                    "source": "dom",
                    "selector": selector,
                    "target": target_name,
                    "predicted_point": clicked_point,
                    "clicked_point": clicked_point,
                    "target_source": "extension" if extension_has_target else "browser_dom",
                    "previous_extension_sequence": previous_sequence,
                    "expected_screen": expected_screen,
                    "action_started_at": action_started_at,
                }
        raise TargetNotFound(target_name)

    async def click_first(self, selectors):
        await self._ensure_started()
        previous_sequence = await self.current_extension_sequence()
        for selector in selectors:
            locator = self._page.locator(selector)
            for index in range(min(await locator.count(), 5)):
                candidate = locator.nth(index)
                if await candidate.is_visible():
                    box = await candidate.bounding_box()
                    action_started_at = monotonic()
                    await candidate.click()
                    clicked_point = (
                        (round(box["x"] + box["width"] / 2),
                         round(box["y"] + box["height"] / 2))
                        if box else None
                    )
                    return {
                        "success": True,
                        "source": "dom",
                        "selector": selector,
                        "predicted_point": clicked_point,
                        "clicked_point": clicked_point,
                        "previous_extension_sequence": previous_sequence,
                        "action_started_at": action_started_at,
                    }
        raise RuntimeError("No visible element matched the reviewed selector set")

    async def click_exact_text(self, text):
        if not text or len(text) > 200:
            raise ValueError("A specific visible target is required")
        await self._ensure_started()
        previous_sequence = await self.current_extension_sequence()
        matches = self._page.get_by_text(text, exact=True)
        visible = []
        for index in range(min(await matches.count(), 10)):
            candidate = matches.nth(index)
            if await candidate.is_visible():
                visible.append(candidate)
        if len(visible) != 1:
            raise RuntimeError(f"Expected one exact visible target, found {len(visible)}")
        box = await visible[0].bounding_box()
        action_started_at = monotonic()
        await visible[0].click()
        clicked_point = (
            (round(box["x"] + box["width"] / 2),
             round(box["y"] + box["height"] / 2))
            if box else None
        )
        return {
            "success": True,
            "source": "dom",
            "target": text,
            "predicted_point": clicked_point,
            "clicked_point": clicked_point,
            "previous_extension_sequence": previous_sequence,
            "action_started_at": action_started_at,
        }

    async def fill_and_submit(self, selectors, text):
        """Fill a visible UI field and submit it without direct URL navigation."""
        await self._ensure_started()
        previous_sequence = await self.current_extension_sequence()
        for selector in selectors:
            locator = self._page.locator(selector)
            for index in range(min(await locator.count(), 5)):
                candidate = locator.nth(index)
                if not await candidate.is_visible():
                    continue
                action_started_at = monotonic()
                await candidate.fill(text)
                await candidate.press("Enter")
                await self._wait_for_ui_ready()
                return {
                    "success": True,
                    "selector": selector,
                    "previous_extension_sequence": previous_sequence,
                    "action_started_at": action_started_at,
                }
        raise RuntimeError("No visible input matched the reviewed selector set")

    async def open_conversation(self, target, selectors, require_unread=False):
        if not target or len(target) > 200:
            raise ValueError("A specific conversation target is required")
        await self._ensure_started()
        await self._page.locator(selectors["conversation_list"][0]).wait_for(state="visible")

        async def matching_rows():
            exact_name = self._page.get_by_text(target, exact=True)
            return self._page.locator(selectors["conversation_item"][0]).filter(has=exact_name)

        rows = await matching_rows()
        count = await rows.count()
        if count == 0:
            search = self._page.locator(selectors["conversation_search"][0]).first
            if not await search.count() or not await search.is_visible():
                raise RuntimeError("Conversation was not visible and the DM search input was unavailable")
            await search.fill(target)
            await self._page.wait_for_timeout(750)
            rows = await matching_rows()
            count = await rows.count()
        if count != 1:
            raise RuntimeError(f"Expected one exact DM conversation, found {count}")

        if require_unread and not await self._conversation_is_explicitly_unread(rows.first):
            raise RuntimeError("The exact conversation is no longer explicitly unread")

        normalized_target = target.casefold().lstrip("@")
        previous_sequence = await self._current_extension_sequence()
        row_box = await rows.first.bounding_box()
        await rows.first.click()
        await self._wait_for_fresh_extension_state(
            previous_sequence,
            expected_screen="conversation",
        )
        await self._page.locator(selectors["chatbox"][0]).wait_for(state="visible")
        header_values = []
        for selector in selectors["chat_target"]:
            locator = self._page.locator(selector)
            for index in range(await locator.count()):
                candidate = locator.nth(index)
                if await candidate.is_visible():
                    header_values.append((await candidate.inner_text()).strip())
        if not any(value.casefold().lstrip("@") == normalized_target for value in header_values):
            raise RuntimeError("Opened chat header does not match the requested conversation")
        clicked_point = (
            (round(row_box["x"] + row_box["width"] / 2),
             round(row_box["y"] + row_box["height"] / 2))
            if row_box else None
        )
        return {
            "success": True,
            "source": "dom",
            "target": target,
            "predicted_point": clicked_point,
            "clicked_point": clicked_point,
            "previous_extension_sequence": previous_sequence,
            "expected_screen": "conversation",
        }

    async def open_unread_conversation(self, target, selectors):
        return await self.open_conversation(target, selectors, require_unread=True)

    async def list_unread_conversations(self, limit, selectors):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
            raise ValueError("Unread conversation limit must be between 1 and 20")
        await self._ensure_started()
        conversation_list = self._page.locator(selectors["conversation_list"][0]).first
        await conversation_list.wait_for(state="visible")
        names = []
        unread_rows = []
        logger.debug(
            "[INBOX] unread_detection row_selector=%s conditions=%s",
            selectors["conversation_item"][0],
            (
                'data-unread in {true,1,yes}; aria-label contains '
                '{unread,new message,chưa đọc,tin nhắn mới}; visible descendant matches '
                '[data-unread="true"],[data-e2e*="unread" i],'
                '[aria-label*="unread" i],[aria-label*="chưa đọc" i]'
            ),
        )
        scroll_script = """(node, args) => {
            let scroller = node;
            for (let depth = 0; scroller && depth < 8; depth += 1) {
                const style = getComputedStyle(scroller);
                if (scroller.scrollHeight > scroller.clientHeight + 4
                    && /(auto|scroll)/.test(`${style.overflowY} ${style.overflow}`)) break;
                scroller = scroller.parentElement;
            }
            scroller = scroller || node;
            if (args.restore !== null) scroller.scrollTop = args.restore;
            else if (args.advance) scroller.scrollTop += Math.max(80, scroller.clientHeight * 0.8);
            return {
                top: Math.round(scroller.scrollTop),
                height: Math.round(scroller.clientHeight),
                total: Math.round(scroller.scrollHeight)
            };
        }"""
        initial = await conversation_list.evaluate(
            scroll_script, {"advance": False, "restore": None}
        )
        pages = 0
        rows_seen = 0
        try:
            while pages < 50 and len(names) < limit:
                rows = self._page.locator(selectors["conversation_item"][0])
                row_count = min(await rows.count(), 100)
                rows_seen = max(rows_seen, row_count)
                for index in range(row_count):
                    row = rows.nth(index)
                    if not await row.is_visible():
                        continue
                    if not await self._conversation_is_explicitly_unread(row):
                        continue
                    name = None
                    for selector in selectors["conversation_nickname"]:
                        candidate = row.locator(selector).first
                        if await candidate.count() and await candidate.is_visible():
                            name = (await candidate.inner_text()).strip()
                            if name:
                                break
                    if name and name not in names:
                        names.append(name)
                        unread_rows.append((name, index))
                    if len(names) >= limit:
                        break
                if len(names) >= limit:
                    break
                before = await conversation_list.evaluate(
                    scroll_script, {"advance": False, "restore": None}
                )
                if before["top"] + before["height"] >= before["total"] - 2:
                    break
                after = await conversation_list.evaluate(
                    scroll_script, {"advance": True, "restore": None}
                )
                if after["top"] <= before["top"]:
                    break
                pages += 1
                await self._page.wait_for_timeout(100)
        finally:
            await conversation_list.evaluate(
                scroll_script, {"advance": False, "restore": initial["top"]}
            )
        logger.info(
            "[INBOX] conversations_found=%s virtual_pages=%s", rows_seen, pages + 1
        )
        logger.info("[INBOX] unread_found=%s", len(names))
        for target, index in unread_rows:
            logger.info("[INBOX] unread target=%s index=%s", target, index)
        return {"success": True, "conversations": names}

    async def list_all_conversations(self, limit, selectors):
        """Every visible conversation name, scrolling to the end or to `limit`.

        Same virtualized-list scroll as `list_unread_conversations`, minus the
        unread filter - every row counts. Used only by the one-time historical
        inbox backfill (`platforms.tiktok.inbox_backfill.InboxBackfillSkill`)
        on a freshly connected account; the ongoing scan still only ever reads
        `list_unread_conversations`.
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("Conversation backfill limit must be between 1 and 500")
        await self._ensure_started()
        conversation_list = self._page.locator(selectors["conversation_list"][0]).first
        await conversation_list.wait_for(state="visible")
        names = []
        scroll_script = """(node, args) => {
            let scroller = node;
            for (let depth = 0; scroller && depth < 8; depth += 1) {
                const style = getComputedStyle(scroller);
                if (scroller.scrollHeight > scroller.clientHeight + 4
                    && /(auto|scroll)/.test(`${style.overflowY} ${style.overflow}`)) break;
                scroller = scroller.parentElement;
            }
            scroller = scroller || node;
            if (args.restore !== null) scroller.scrollTop = args.restore;
            else if (args.advance) scroller.scrollTop += Math.max(80, scroller.clientHeight * 0.8);
            return {
                top: Math.round(scroller.scrollTop),
                height: Math.round(scroller.clientHeight),
                total: Math.round(scroller.scrollHeight)
            };
        }"""
        initial = await conversation_list.evaluate(
            scroll_script, {"advance": False, "restore": None}
        )
        pages = 0
        try:
            while pages < 200 and len(names) < limit:
                rows = self._page.locator(selectors["conversation_item"][0])
                row_count = min(await rows.count(), 100)
                for index in range(row_count):
                    row = rows.nth(index)
                    if not await row.is_visible():
                        continue
                    name = None
                    for selector in selectors["conversation_nickname"]:
                        candidate = row.locator(selector).first
                        if await candidate.count() and await candidate.is_visible():
                            value = (await candidate.inner_text()).strip()
                            if value:
                                name = value
                                break
                    if name and name not in names:
                        names.append(name)
                    if len(names) >= limit:
                        break
                if len(names) >= limit:
                    break
                before = await conversation_list.evaluate(
                    scroll_script, {"advance": False, "restore": None}
                )
                if before["top"] + before["height"] >= before["total"] - 2:
                    break
                after = await conversation_list.evaluate(
                    scroll_script, {"advance": True, "restore": None}
                )
                if after["top"] <= before["top"]:
                    break
                pages += 1
                await self._page.wait_for_timeout(100)
        finally:
            await conversation_list.evaluate(
                scroll_script, {"advance": False, "restore": initial["top"]}
            )
        logger.info(
            "[INBOX] backfill conversations_found=%s virtual_pages=%s", len(names), pages + 1
        )
        return {"success": True, "conversations": names}

    async def detect_message_requests(self, selectors):
        """Read the pending-request navigation row without opening it."""
        await self._ensure_started()
        for selector in selectors["message_requests_button"]:
            candidates = self._page.locator(selector)
            for index in range(min(await candidates.count(), 10)):
                candidate = candidates.nth(index)
                if not await candidate.is_visible():
                    continue
                value = " ".join(filter(None, (
                    await candidate.get_attribute("aria-label"),
                    await candidate.inner_text(),
                )))
                counts = [int(item) for item in re.findall(r"\d+", value)]
                count = max(counts, default=1)
                return {
                    "success": True,
                    "present": True,
                    "count": count,
                    "selector": selector,
                }
        return {"success": True, "present": False, "count": 0}

    async def open_message_requests(self, selectors):
        """Enter TikTok's request sub-list, without opening a customer row."""
        await self._ensure_started()
        previous_sequence = await self.current_extension_sequence()
        for selector in selectors["message_requests_button"]:
            candidates = self._page.locator(selector)
            for index in range(min(await candidates.count(), 10)):
                candidate = candidates.nth(index)
                if not await candidate.is_visible():
                    continue
                await candidate.click()
                await self._page.locator(
                    selectors["conversation_list"][0]
                ).wait_for(state="visible")
                await self._page.wait_for_timeout(250)
                return {
                    "success": True,
                    "source": "dom",
                    "previous_extension_sequence": previous_sequence,
                }
        raise RuntimeError("TikTok Message requests button was not found")

    async def list_message_requests(self, limit, selectors):
        """List request rows; every row in this sub-list is still pending."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
            raise ValueError("Message request limit must be between 1 and 20")
        await self._ensure_started()
        await self._page.locator(
            selectors["conversation_list"][0]
        ).first.wait_for(state="visible")
        rows = self._page.locator(selectors["conversation_item"][0])
        names = []
        for index in range(min(await rows.count(), 100)):
            row = rows.nth(index)
            if not await row.is_visible():
                continue
            name = ""
            for selector in selectors["conversation_nickname"]:
                candidate = row.locator(selector).first
                if await candidate.count() and await candidate.is_visible():
                    name = (await candidate.inner_text()).strip()
                    if name:
                        break
            if name and name not in names:
                names.append(name)
            if len(names) >= limit:
                break
        logger.info("[INBOX] message_requests_found=%s", len(names))
        return {"success": True, "conversations": names}

    async def open_message_request(self, target, selectors):
        """Open one exact pending request without assuming a composer exists."""
        if not target or len(target) > 200:
            raise ValueError("A specific message request target is required")
        await self._ensure_started()
        exact_name = self._page.get_by_text(target, exact=True)
        rows = self._page.locator(
            selectors["conversation_item"][0]
        ).filter(has=exact_name)
        if await rows.count() != 1:
            raise RuntimeError(
                f"Expected one exact Message request, found {await rows.count()}"
            )
        previous_sequence = await self.current_extension_sequence()
        await rows.first.click()
        await self._page.wait_for_timeout(250)

        # Pending requests may not expose the normal composer until accepted.
        # Identity must still be proved by the active chat header.
        normalized = target.casefold().lstrip("@")
        header_values = []
        for selector in selectors["chat_target"]:
            headers = self._page.locator(selector)
            for index in range(min(await headers.count(), 10)):
                header = headers.nth(index)
                if await header.is_visible():
                    header_values.append((await header.inner_text()).strip())
        if not any(value.casefold().lstrip("@") == normalized for value in header_values):
            raise RuntimeError("Opened Message request header does not match the target")
        return {
            "success": True,
            "source": "dom",
            "target": target,
            "previous_extension_sequence": previous_sequence,
        }

    async def accept_message_request(self, target, selectors):
        """Accept the current exact request and wait until replying is possible."""
        await self._ensure_started()
        normalized = str(target or "").strip().casefold().lstrip("@")
        header_values = []
        for selector in selectors["chat_target"]:
            headers = self._page.locator(selector)
            for index in range(min(await headers.count(), 10)):
                header = headers.nth(index)
                if await header.is_visible():
                    header_values.append((await header.inner_text()).strip())
        if not normalized or not any(
            value.casefold().lstrip("@") == normalized for value in header_values
        ):
            raise RuntimeError(
                "Current Message request header does not match the Accept target"
            )
        previous_sequence = await self.current_extension_sequence()
        # Accessible-name equality is the primary contract. `has-text` is not
        # safe here: it can match an ancestor or a longer destructive label.
        # Report/Delete may be visible beside Accept, but can never enter this
        # exact-name candidate set.
        exact = self._page.get_by_role(
            "button", name=re.compile(r"^(Accept|Chấp nhận)$", re.IGNORECASE)
        )
        exact_visible = []
        for index in range(min(await exact.count(), 10)):
            candidate = exact.nth(index)
            if await candidate.is_visible() and await candidate.is_enabled():
                exact_visible.append(candidate)
        if len(exact_visible) > 1:
            raise RuntimeError(
                f"Expected one exact Accept button, found {len(exact_visible)}"
            )
        if exact_visible:
            candidate = exact_visible[0]
            await candidate.click()
            await self._page.locator(
                selectors["chatbox"][0]
            ).wait_for(state="visible")
            return {
                "success": True,
                "source": "dom_exact_accessible_name",
                "target": target,
                "previous_extension_sequence": previous_sequence,
                "expected_screen": "conversation",
            }

        # TikTok sometimes omits the accessible role but keeps a semantic data
        # attribute. Even then, the visible/ARIA label must equal Accept; a
        # selector match alone is not authority to click.
        for selector in selectors["message_request_accept"]:
            candidates = self._page.locator(selector)
            for index in range(min(await candidates.count(), 10)):
                candidate = candidates.nth(index)
                if not await candidate.is_visible() or not await candidate.is_enabled():
                    continue
                label = str(await candidate.get_attribute("aria-label") or "").strip()
                visible_text = str(await candidate.inner_text() or "").strip()
                normalized_labels = {
                    re.sub(r"\s+", " ", value).casefold()
                    for value in (label, visible_text) if value
                }
                if not normalized_labels.intersection({"accept", "chấp nhận"}):
                    logger.warning(
                        "[INBOX] rejected unsafe Accept selector=%s labels=%s",
                        selector, sorted(normalized_labels),
                    )
                    continue
                await candidate.click()
                await self._page.locator(
                    selectors["chatbox"][0]
                ).wait_for(state="visible")
                return {
                    "success": True,
                    "source": "dom",
                    "target": target,
                    "previous_extension_sequence": previous_sequence,
                    "expected_screen": "conversation",
                }
        raise RuntimeError("TikTok Message request Accept button was not found")

    async def return_to_primary_inbox(self, selectors):
        """Leave the request sub-list, stopping as soon as its nav row returns."""
        await self._ensure_started()
        for _ in range(3):
            detected = await self.detect_message_requests(selectors)
            if detected["present"]:
                return {"success": True, "source": "dom", "already_primary": True}
            clicked = False
            for selector in selectors["message_requests_back"]:
                candidates = self._page.locator(selector)
                for index in range(min(await candidates.count(), 10)):
                    candidate = candidates.nth(index)
                    if await candidate.is_visible():
                        try:
                            await candidate.click(timeout=min(self.timeout_ms, 1500))
                        except Exception:
                            # TikTok's request-list header briefly leaves an
                            # invisible animation cover above this exact,
                            # test-id-addressed Back button. Force is safe only
                            # after the same node is still visible and enabled;
                            # no coordinate or fuzzy target is involved.
                            if not await candidate.is_visible() or not await candidate.is_enabled():
                                raise
                            await candidate.click(force=True)
                        await self._page.wait_for_timeout(250)
                        clicked = True
                        break
                if clicked:
                    break
            if not clicked:
                break
        detected = await self.detect_message_requests(selectors)
        if detected["present"]:
            return {"success": True, "source": "dom", "already_primary": False}
        raise RuntimeError("Could not return from Message requests to the primary inbox")

    @staticmethod
    async def _conversation_is_explicitly_unread(row):
        unread_value = str(await row.get_attribute("data-unread") or "").casefold()
        if unread_value in {"true", "1", "yes"}:
            return True
        label = str(await row.get_attribute("aria-label") or "").casefold()
        unread_words = ("unread", "new message", "chưa đọc", "tin nhắn mới")
        if any(word in label for word in unread_words):
            return True
        signals = row.locator(
            '[data-unread="true"],[data-e2e*="unread" i],'
            '[aria-label*="unread" i],[aria-label*="chưa đọc" i]'
        )
        for index in range(min(await signals.count(), 10)):
            if await signals.nth(index).is_visible():
                return True
        return False

    async def list_conversations(self, limit, selectors):
        """Return exact visible conversation names in their current UI order."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 5:
            raise ValueError("Conversation preview limit must be between 1 and 5")
        await self._ensure_started()
        conversation_list = self._page.locator(selectors["conversation_list"][0]).first
        await conversation_list.wait_for(state="visible")
        rows = self._page.locator(selectors["conversation_item"][0])
        names = []
        for index in range(min(await rows.count(), 20)):
            row = rows.nth(index)
            if not await row.is_visible():
                continue
            name = None
            for selector in selectors["conversation_nickname"]:
                candidate = row.locator(selector).first
                if await candidate.count() and await candidate.is_visible():
                    value = (await candidate.inner_text()).strip()
                    if value:
                        name = value
                        break
            if name and name not in names:
                names.append(name)
            if len(names) >= limit:
                break
        if len(names) < limit:
            raise RuntimeError(
                f"Only {len(names)} exact visible conversations were available for a {limit}-item preview"
            )
        return {"success": True, "conversations": names}

    async def input_message(
        self, text, selectors, visual_target_id=None, pre_input=None,
    ):
        if not text or len(text) > 1000 or any(ord(char) < 32 and char not in "\n\t" for char in text):
            raise ValueError("Message must contain 1-1000 printable characters")
        await self._ensure_started()
        for selector in selectors:
            locator = self._page.locator(selector).first
            if await locator.count() and await locator.is_visible():
                if pre_input is not None:
                    await pre_input()
                previous_sequence = await self._current_extension_sequence()
                await locator.fill(text)
                await self._wait_for_fresh_extension_state(
                    previous_sequence,
                    expected_screen="conversation",
                    expected_events={"composer_changed"},
                )
                return {"success": True, "text": text}
        target = self._visual_targets.get(visual_target_id)
        if not target or target.type != "message_input":
            raise RuntimeError("Visible message input was not found")
        if target.confidence < self._visual_target_threshold:
            raise RuntimeError("Visual message input confidence is below threshold")
        active_is_editor = await self._page.evaluate(
            """() => {
                const node = document.activeElement;
                return !!node && (
                    node.isContentEditable || node.getAttribute('role') === 'textbox'
                    || node.tagName === 'TEXTAREA'
                );
            }"""
        )
        if not active_is_editor:
            raise RuntimeError("The confirmed visual message input is not focused")
        if pre_input is not None:
            await pre_input()
        previous_sequence = await self._current_extension_sequence()
        await self._page.keyboard.insert_text(text)
        await self._wait_for_fresh_extension_state(
            previous_sequence,
            expected_screen="conversation",
            expected_events={"composer_changed"},
        )
        return {"success": True, "text": text, "target_id": visual_target_id}

    async def send_message(
        self, text, input_selectors, message_selectors, visual_target_id=None,
        send_target=None, action_executor=None, pre_send=None,
    ):
        if not text:
            raise ValueError("A verified non-empty message is required")
        await self._ensure_started()
        editor = None
        for selector in input_selectors:
            candidate = self._page.locator(selector).first
            if await candidate.count() and await candidate.is_visible():
                editor = candidate
                break
        if editor is None:
            target = self._visual_targets.get(visual_target_id)
            if not target or target.type != "message_input":
                raise RuntimeError("Visible message editor was not found")
            current = (await self._active_editor_text()).strip()
        else:
            current = (await editor.inner_text()).strip()
        if current != text:
            raise RuntimeError("Composer text changed before send; refusing to click Send")

        messages = self._page.locator(message_selectors[0]).filter(has_text=text)

        async def exact_visible_count():
            count = 0
            for index in range(await messages.count()):
                candidate = messages.nth(index)
                if await candidate.is_visible() and (await candidate.inner_text()).strip() == text:
                    count += 1
            return count

        before_count = await exact_visible_count()
        previous_sequence = await self._current_extension_sequence()
        if pre_send is not None:
            await pre_send()
        if send_target is not None and action_executor is not None:
            click_result = await action_executor.click(
                send_target, expected_screen="conversation"
            )
        else:
            # Compatibility for non-TikTok adapters. Production TikTok Web
            # always supplies a resolved Send target.
            if editor is None:
                await self._page.keyboard.press("Enter")
            else:
                await editor.press("Enter")
            click_result = {
                "success": True,
                "source": "dom",
                "target": "message_input",
                "click_method": "keyboard_compatibility",
            }
        await self._wait_for_fresh_extension_state(
            previous_sequence,
            expected_screen="conversation",
            expected_events={"message_new", "composer_changed"},
        )
        remaining = (
            await self._active_editor_text() if editor is None else await editor.inner_text()
        )
        after_count = await exact_visible_count()
        self._last_send_evidence = {
            "expected_text": text,
            "before_exact_count": before_count,
            "after_exact_count": after_count,
            "composer_text": remaining.strip(),
            "message_count_increased": after_count > before_count,
        }
        # Send was clicked exactly once. Delivery is classified by the
        # observer; ambiguous evidence must not make this tool click again.
        return {**click_result, "success": True, "attempted": True}

    async def attach_message_media(
        self, path, selectors, preview_selectors=(), uploading_selectors=(),
        error_selectors=(), button_selectors=(), confirm_selectors=(),
        dialog_selectors=(), recipient_gate=None,
    ):
        """Stage one local catalogue file in TikTok's verified DM composer."""
        unresolved = Path(str(path or ""))
        if unresolved.is_symlink():
            raise ValueError("Attachment symlinks are not allowed")
        candidate = unresolved.resolve()
        if not any(candidate.is_relative_to(root) for root in self.allowed_media_roots):
            raise ValueError("Attachment is outside the configured media roots")
        if not candidate.is_file():
            raise ValueError("Attachment file does not exist")
        suffix = candidate.suffix.casefold()
        image_suffixes = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
        video_suffixes = {".mp4", ".mov", ".webm", ".m4v"}
        if suffix not in image_suffixes | video_suffixes:
            raise ValueError("Attachment type is not allowed")
        with candidate.open("rb") as stream:
            header = stream.read(16)
        signatures = {
            ".jpg": header.startswith(b"\xff\xd8\xff"),
            ".jpeg": header.startswith(b"\xff\xd8\xff"),
            ".png": header.startswith(b"\x89PNG\r\n\x1a\n"),
            ".gif": header.startswith((b"GIF87a", b"GIF89a")),
            ".webp": header.startswith(b"RIFF") and header[8:12] == b"WEBP",
            ".mp4": header[4:8] == b"ftyp",
            ".mov": header[4:8] == b"ftyp",
            ".m4v": header[4:8] == b"ftyp",
            ".webm": header.startswith(b"\x1aE\xdf\xa3"),
        }
        if not signatures[suffix]:
            raise ValueError("Attachment content does not match its file extension")
        mime_by_suffix = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".webp": "image/webp", ".gif": "image/gif", ".mp4": "video/mp4",
            ".mov": "video/quicktime", ".m4v": "video/mp4", ".webm": "video/webm",
        }
        mime = mime_by_suffix[suffix]
        if self.allowed_media_mime_types and mime not in self.allowed_media_mime_types:
            raise ValueError("Attachment MIME type is not allowed by configuration")
        media_kind = "video" if suffix in video_suffixes else "image"
        size_limit = (
            self.max_video_size_bytes if media_kind == "video"
            else self.max_image_size_bytes
        )
        if candidate.stat().st_size > size_limit:
            raise ValueError(f"Attachment exceeds the configured {media_kind} size limit")
        if media_kind == "video":
            duration = self._probe_video_duration(candidate)
            if duration is None:
                raise ValueError("Video duration could not be verified")
            if duration > self.max_dm_video_duration_seconds:
                raise ValueError(
                    "Video exceeds the configured TikTok DM duration limit"
                )

        await self._ensure_started()
        previous_sequence = await self._current_extension_sequence()
        async def stage_from_input():
            for selector in selectors:
                candidates = self._page.locator(selector)
                for index in range(await candidates.count()):
                    locator = candidates.nth(index)
                    if await locator.is_disabled():
                        continue
                    accepts = (
                        await locator.get_attribute("accept") or ""
                    ).casefold()
                    if (
                        accepts
                        and "*/*" not in accepts
                        and media_kind not in accepts
                    ):
                        continue
                    if recipient_gate is not None:
                        await recipient_gate()
                    await locator.set_input_files(str(candidate))
                    return await self._wait_for_media_staged(
                        locator, candidate.name, media_kind,
                        preview_selectors, uploading_selectors, error_selectors,
                        dialog_selectors, confirm_selectors,
                    )
            return None

        stage = await stage_from_input()
        source = "file_input"
        if stage is None and button_selectors:
            # Some TikTok builds create the file input only while handling a
            # click on the image button. Catch the chooser event so no native
            # dialog or filesystem path is typed into the page.
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError

            chooser_timeout = max(500, min(self.timeout_ms, 3000))
            for selector in button_selectors:
                buttons = self._page.locator(selector)
                for index in range(await buttons.count()):
                    button = buttons.nth(index)
                    if not await button.is_visible() or await button.is_disabled():
                        continue
                    if recipient_gate is not None:
                        await recipient_gate()
                    try:
                        async with self._page.expect_file_chooser(
                            timeout=chooser_timeout
                        ) as chooser_info:
                            await button.click()
                        chooser = await chooser_info.value
                    except PlaywrightTimeoutError:
                        # A custom control can add its input after the click
                        # without raising a native chooser event.
                        stage = await stage_from_input()
                        if stage is not None:
                            source = "media_button_input"
                            break
                        continue
                    await chooser.set_files(str(candidate))
                    stage = await self._wait_for_media_staged(
                        chooser.element, candidate.name, media_kind,
                        preview_selectors, uploading_selectors, error_selectors,
                        dialog_selectors, confirm_selectors,
                    )
                    source = "file_chooser"
                    break
                if stage is not None:
                    break

        if stage is None:
            raise RuntimeError(
                "TikTok media button and attachment input were not found"
            )
        confirmation = None
        if confirm_selectors:
            if recipient_gate is not None:
                await recipient_gate()
            confirmation = await self._confirm_message_media(
                confirm_selectors, dialog_selectors,
            )
        return {
            "success": True,
            "name": candidate.name,
            "kind": media_kind,
            "mime": mime,
            "sha256": self._sha256_file(candidate),
            "previous_extension_sequence": previous_sequence,
            "stage_evidence": stage,
            "source": source,
            "confirmation": confirmation,
        }

    # Hosts a DM picture can never legitimately live on. `src` comes out of
    # the page's DOM, so it is attacker-influenced input in the same way any
    # scraped URL is: fetching it server-side without this check would turn
    # the agent into a probe for whatever listens on the server's own network.
    _PRIVATE_MEDIA_HOSTS = (
        "localhost", "127.", "0.", "10.", "169.254.", "192.168.", "[::1]", "::1",
    )

    @classmethod
    def _media_url_allowed(cls, url):
        """True for a picture URL that is safe to fetch from this server."""
        text = str(url or "").strip()
        if text.startswith("blob:"):
            # Only resolvable inside the page that made it, so it never leaves
            # the browser and cannot address anything on the network.
            return True
        if not text.startswith("https://"):
            return False
        host = urlparse(text).hostname or ""
        host = host.strip().casefold()
        if not host or any(host.startswith(prefix) for prefix in cls._PRIVATE_MEDIA_HOSTS):
            return False
        if host.startswith("172."):
            try:
                second = int(host.split(".")[1])
            except (IndexError, ValueError):
                return False
            if 16 <= second <= 31:
                return False
        return True

    async def fetch_media_bytes(self, url, *, timeout_ms=None, max_bytes=0):
        """Download one picture the open thread is showing.

        Two transports, because the two kinds of URL in a DM thread cannot be
        read the same way:

        * a `blob:` URL only resolves inside the document that created it, so
          it is read with `fetch()` in the page itself. This is the only way
          to get at an image the agent has just uploaded.
        * an https CDN URL goes through the browser context's request API,
          which carries the session cookies and is not subject to the page's
          CORS rules. Both matter: TikTok serves DM pictures from a
          `-private` host that answers 403 without the session, and the page
          could not fetch a cross-origin CDN itself.

        Returns `(data, content_type)`, and `(b"", "")` when the picture could
        not be downloaded - a picture nobody can fetch is never a reason to
        fail the scan that found it.
        """
        text = str(url or "").strip()
        if not text or not self._media_url_allowed(text):
            logger.info("[MEDIA] refused url_scheme_or_host_not_allowed")
            return b"", ""
        await self._ensure_started()
        budget = int(timeout_ms or self.timeout_ms)
        try:
            if text.startswith("blob:"):
                return await self._fetch_blob_bytes(text, max_bytes)
            response = await self._page.request.get(
                text,
                headers={"referer": f"{self.base_url}/"},
                timeout=budget,
            )
            if not response.ok:
                logger.info(
                    "[MEDIA] download rejected status=%s", response.status,
                )
                return b"", ""
            data = await response.body()
            if max_bytes and len(data) > max_bytes:
                logger.info(
                    "[MEDIA] download too large bytes=%s limit=%s",
                    len(data), max_bytes,
                )
                return b"", ""
            content_type = str(
                response.headers.get("content-type", "") or ""
            ).split(";")[0].strip().casefold()
            return data, content_type
        except Exception as exc:
            logger.info(
                "[MEDIA] download failed error_type=%s", type(exc).__name__, exc_info=True,
            )
            return b"", ""

    async def _fetch_blob_bytes(self, url, max_bytes):
        """Read a `blob:` URL through the page that owns it."""
        payload = await self._page.evaluate(
            """async (url) => {
                try {
                    const response = await fetch(url);
                    if (!response.ok) return null;
                    const buffer = await response.arrayBuffer();
                    const bytes = new Uint8Array(buffer);
                    let binary = '';
                    for (let index = 0; index < bytes.length; index += 1) {
                        binary += String.fromCharCode(bytes[index]);
                    }
                    return {
                        data: btoa(binary),
                        type: (response.headers.get('content-type') || ''),
                        size: bytes.length,
                    };
                } catch (error) {
                    return null;
                }
            }""",
            url,
        )
        if not payload or not payload.get("data"):
            logger.info("[MEDIA] blob download returned nothing")
            return b"", ""
        if max_bytes and int(payload.get("size") or 0) > max_bytes:
            logger.info(
                "[MEDIA] blob too large bytes=%s limit=%s",
                payload.get("size"), max_bytes,
            )
            return b"", ""
        return (
            base64.b64decode(payload["data"]),
            str(payload.get("type") or "").split(";")[0].strip().casefold(),
        )

    async def clear_message_draft(
        self, input_selectors, media_input_selectors=(), preview_selectors=(),
        dialog_selectors=(), cancel_selectors=(), remove_selectors=(),
    ):
        """Remove all unsent text and staged media, then verify the draft is empty."""
        await self._ensure_started()
        previous_sequence = await self._current_extension_sequence()
        mutated = False

        for selector in cancel_selectors:
            candidates = self._page.locator(selector)
            for index in range(await candidates.count()):
                candidate = candidates.nth(index)
                if await candidate.is_visible() and not await candidate.is_disabled():
                    await candidate.click()
                    mutated = True

        # Escape closes media dialogs in builds that expose no accessible close
        # control. It never submits the composer.
        for selector in dialog_selectors:
            dialogs = self._page.locator(selector)
            for index in range(await dialogs.count()):
                if await dialogs.nth(index).is_visible():
                    await self._page.keyboard.press("Escape")
                    mutated = True
                    break

        for selector in remove_selectors:
            candidates = self._page.locator(selector)
            for index in range(await candidates.count() - 1, -1, -1):
                candidate = candidates.nth(index)
                if await candidate.is_visible() and not await candidate.is_disabled():
                    await candidate.click()
                    mutated = True

        for selector in media_input_selectors:
            candidates = self._page.locator(selector)
            for index in range(await candidates.count()):
                candidate = candidates.nth(index)
                file_count = await candidate.evaluate(
                    "node => node.files ? node.files.length : 0"
                )
                if file_count:
                    await candidate.set_input_files([])
                    mutated = True

        editor = None
        editor_text = ""
        for selector in input_selectors:
            candidate = self._page.locator(selector).first
            if await candidate.count() and await candidate.is_visible():
                editor = candidate
                editor_text = str(await editor.evaluate(
                    """node => node.isContentEditable
                        ? (node.innerText || node.textContent || '')
                        : ('value' in node ? node.value : (node.innerText || ''))"""
                ) or "").strip()
                if editor_text:
                    await editor.fill("")
                    mutated = True
                break
        if editor is None:
            raise RuntimeError("Message composer was not found while clearing the draft")

        if mutated:
            await self._wait_for_fresh_extension_state(
                previous_sequence,
                expected_screen="conversation",
                expected_events={"composer_changed"},
                timeout=2.0,
            )

        deadline = monotonic() + 2.0
        state = await self._read_draft_state(
            input_selectors, media_input_selectors, preview_selectors,
            dialog_selectors, editor=editor,
        )
        while not state["empty"] and monotonic() < deadline:
            await self._page.wait_for_timeout(100)
            state = await self._read_draft_state(
                input_selectors, media_input_selectors, preview_selectors,
                dialog_selectors, editor=editor,
            )
        if not state["empty"]:
            raise RuntimeError(
                "Message draft could not be verified as empty: "
                f"text_present={bool(state['text'])} "
                f"dialog_visible={state['dialog_visible']} "
                f"preview_visible={state['preview_visible']} "
                f"file_count={state['file_count']} "
                f"dialog_selector={state['dialog_selector'] or 'none'} "
                f"preview_selector={state['preview_selector'] or 'none'}"
            )
        return {
            "success": True, "draft_empty": True, "media_empty": True,
            "mutated": mutated,
            "previous_extension_sequence": previous_sequence,
        }

    async def inspect_message_draft(
        self, input_selectors, media_input_selectors=(), preview_selectors=(),
        dialog_selectors=(),
    ):
        """What the composer holds right now, without touching anything.

        The read-only twin of `clear_message_draft`'s own verification: both
        answer "is this draft empty" through `_read_draft_state`, so there is
        one implementation and one set of selectors behind both. Callers use
        it to refuse to type into a composer that still holds media from an
        aborted reply - `fill()` overwrites leftover text, but a staged image
        nobody asked for would ride along with the next send.

        `preview_selectors` must be the draft-only tray/dialog ones
        (`message_media_draft_preview`), never the broad
        `message_media_preview`: sent DM images are blob URLs too.
        """
        await self._ensure_started()
        state = await self._read_draft_state(
            input_selectors, media_input_selectors, preview_selectors,
            dialog_selectors,
        )
        return {
            "success": True,
            "empty": state["empty"],
            "text_present": bool(state["text"]),
            "characters": len(state["text"]),
            "file_count": state["file_count"],
            "preview_visible": state["preview_visible"],
            "dialog_visible": state["dialog_visible"],
            # Text alone is harmless - the next `fill()` replaces it. Staged
            # media is not: nothing in the send path replaces a file the agent
            # did not put there.
            "media_present": bool(
                state["file_count"]
                or state["preview_visible"]
                or state["dialog_visible"]
            ),
            "dialog_selector": state["dialog_selector"],
            "preview_selector": state["preview_selector"],
        }

    async def _read_draft_state(
        self, input_selectors, media_input_selectors, preview_selectors,
        dialog_selectors, editor=None,
    ):
        """One read-only pass over the composer: text, staged files, trays.

        `editor` lets a caller that already resolved the composer reuse it.
        Nothing here clicks, fills, presses a key or navigates.
        """
        if editor is None:
            for selector in input_selectors:
                candidate = self._page.locator(selector).first
                if await candidate.count() and await candidate.is_visible():
                    editor = candidate
                    break
        if editor is None:
            raise RuntimeError("Message composer was not found while reading the draft")
        text = str(await editor.evaluate(
            """node => node.isContentEditable
                ? (node.innerText || node.textContent || '')
                : ('value' in node ? node.value : (node.innerText || ''))"""
        ) or "").strip()
        dialog_visible = False
        dialog_selector = ""
        for selector in dialog_selectors:
            candidates = self._page.locator(selector)
            if any([
                await candidates.nth(index).is_visible()
                for index in range(await candidates.count())
            ]):
                dialog_visible = True
                dialog_selector = selector
                break
        preview_visible = False
        preview_selector = ""
        for selector in preview_selectors:
            candidates = self._page.locator(selector)
            for index in range(await candidates.count()):
                if not await candidates.nth(index).is_visible():
                    continue
                preview_visible = True
                preview_selector = selector
                break
            if preview_visible:
                break
        file_count = 0
        for selector in media_input_selectors:
            candidates = self._page.locator(selector)
            for index in range(await candidates.count()):
                file_count += int(await candidates.nth(index).evaluate(
                    "node => node.files ? node.files.length : 0"
                ))
        return {
            "text": text,
            "dialog_visible": dialog_visible,
            "dialog_selector": dialog_selector,
            "preview_visible": preview_visible,
            "preview_selector": preview_selector,
            "file_count": file_count,
            "empty": not (
                text or dialog_visible or preview_visible or file_count
            ),
        }

    @staticmethod
    def _sha256_file(path):
        import hashlib

        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _probe_video_duration(path):
        """Read duration without invoking a shell or trusting a filename."""
        try:
            completed = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "json", str(path),
                ],
                check=True, capture_output=True, text=True, timeout=5,
            )
            value = json.loads(completed.stdout).get("format", {}).get("duration")
            return float(value) if value is not None else None
        except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError):
            return None

    async def _confirm_message_media(self, confirm_selectors, dialog_selectors):
        """Press TikTok's explicit media Send and prove its modal closed."""
        async def enabled_confirm():
            for selector in confirm_selectors:
                candidates = self._page.locator(selector)
                for index in range(await candidates.count()):
                    candidate = candidates.nth(index)
                    if await candidate.is_visible() and not await candidate.is_disabled():
                        return candidate
            return None

        # An image counts as staged once the file input holds it, which can be
        # before TikTok has mounted its "Send media" dialog - a single look
        # here failed an ordinary image send with "button was not found".
        # Nothing has been clicked yet, so waiting cannot send anything twice.
        deadline = monotonic() + min(15.0, self.media_stage_timeout_ms / 1000.0)
        confirm = await enabled_confirm()
        while confirm is None and monotonic() < deadline:
            await self._page.wait_for_timeout(200)
            confirm = await enabled_confirm()
        if confirm is None:
            # TikTok keeps a disabled Send button when a selected file violates
            # a client-side media rule (for example, DM videos over 15 seconds).
            # Surface that reason instead of misreporting a missing selector.
            dialogs = self._page.locator('[role="dialog"]')
            for index in range(await dialogs.count()):
                dialog = dialogs.nth(index)
                if not await dialog.is_visible():
                    continue
                message = " ".join((await dialog.inner_text()).split())[:300]
                if message:
                    raise RuntimeError(f"TikTok rejected the attachment: {message}")
            raise RuntimeError("TikTok media confirmation button was not found")

        previous_sequence = await self._current_extension_sequence()
        await confirm.click()
        deadline = monotonic() + min(5.0, self.media_stage_timeout_ms / 1000.0)
        while monotonic() < deadline:
            modal_visible = False
            for selector in dialog_selectors:
                dialogs = self._page.locator(selector)
                for index in range(await dialogs.count()):
                    if await dialogs.nth(index).is_visible():
                        modal_visible = True
                        break
                if modal_visible:
                    break
            if not modal_visible:
                fresh = await self._wait_for_fresh_extension_state(
                    previous_sequence,
                    expected_screen="conversation",
                    expected_events="message_sent",
                    timeout=2.0,
                )
                return {
                    "clicked": True,
                    "modal_closed": True,
                    "fresh_state": fresh is not None,
                }
            await self._page.wait_for_timeout(100)
        raise RuntimeError(
            "TikTok media confirmation was clicked but the modal stayed open"
        )

    async def _wait_for_media_staged(
        self, locator, filename, media_kind, preview_selectors,
        uploading_selectors, error_selectors, dialog_selectors=(),
        confirm_selectors=(),
    ):
        """Wait until TikTok retained the file or rendered a stable preview.

        TikTok shows a picked file in its "Send media" dialog, which is
        mounted outside the chatbox the file input lives in. A video is only
        ready once it has a preview, so the preview is looked for in that
        dialog too - with its Send enabled, since TikTok keeps it disabled
        while the video is still processing.
        """
        deadline = monotonic() + self.media_stage_timeout_ms / 1000.0
        stable_count = 0
        last_state = None
        while monotonic() < deadline:
            state = await locator.evaluate(
                """(node, args) => {
                    let root = node.closest('[data-e2e="dm-new-chatbox"],'
                        + '[data-e2e="message-input-area"],form');
                    if (!root) {
                        root = node.parentElement;
                        for (let i = 0; root && i < 4; i += 1) root = root.parentElement;
                    }
                    root = root || document;
                    const visible = value => {
                        const box = value.getBoundingClientRect();
                        const style = getComputedStyle(value);
                        return box.width > 0 && box.height > 0
                            && style.display !== 'none' && style.visibility !== 'hidden';
                    };
                    const firstVisible = selectors => {
                        for (const selector of selectors) {
                            try {
                                const found = [...root.querySelectorAll(selector)].find(visible);
                                if (found) return found;
                            } catch (_) {}
                        }
                        return null;
                    };
                    const file = node.files && node.files.length === 1 ? node.files[0] : null;
                    const dialogs = [];
                    for (const selector of args.dialogs) {
                        try {
                            for (const found of document.querySelectorAll(selector)) {
                                if (visible(found) && !dialogs.includes(found)) dialogs.push(found);
                            }
                        } catch (_) {}
                    }
                    const inDialog = dialogs.some(dialog => {
                        const hasPreview = args.preview.some(selector => {
                            try {
                                return [...dialog.querySelectorAll(selector)].some(visible);
                            } catch (_) { return false; }
                        });
                        if (!hasPreview) return false;
                        if (!args.confirm.length) return true;
                        return args.confirm.some(selector => {
                            try {
                                return [...document.querySelectorAll(selector)].some(button =>
                                    dialog.contains(button) && visible(button)
                                    && !button.disabled
                                    && button.getAttribute('aria-disabled') !== 'true');
                            } catch (_) { return false; }
                        });
                    });
                    const preview = firstVisible(args.preview) || (inDialog ? dialogs[0] : null);
                    const busy = firstVisible(args.uploading);
                    const error = firstVisible(args.errors);
                    const previewKind = preview && preview.tagName
                        ? preview.tagName.toLowerCase() : '';
                    return {
                        retained: Boolean(file && file.name === args.filename),
                        preview: Boolean(preview),
                        preview_kind: previewKind,
                        busy: Boolean(busy),
                        error: error ? (error.innerText || error.textContent || 'upload error').trim().slice(0, 200) : ''
                    };
                }""",
                {
                    "filename": filename,
                    "kind": media_kind,
                    "preview": list(preview_selectors),
                    "uploading": list(uploading_selectors),
                    "errors": list(error_selectors),
                    "dialogs": list(dialog_selectors),
                    "confirm": list(confirm_selectors),
                },
            )
            if state.get("error"):
                raise RuntimeError(f"TikTok rejected the attachment: {state['error']}")
            # A video is not ready merely because the file input still holds
            # it: TikTok must render a preview and remain out of an uploading
            # state. Images may legitimately stay in the native file input.
            ready = not state.get("busy") and (
                state.get("preview")
                if media_kind == "video"
                else state.get("retained") or state.get("preview")
            )
            stable_count = stable_count + 1 if ready else 0
            last_state = state
            if stable_count >= (6 if media_kind == "video" else 3):
                return state
            await self._page.wait_for_timeout(250)
        raise RuntimeError(
            f"TikTok did not stage the {media_kind} attachment before timeout; "
            f"last_state={last_state}"
        )

    async def _active_editor_text(self):
        return await self._page.evaluate(
            """() => {
                const node = document.activeElement;
                if (!node || !(
                    node.isContentEditable || node.getAttribute('role') === 'textbox'
                    || node.tagName === 'TEXTAREA'
                )) return '';
                return node.value ?? node.innerText ?? node.textContent ?? '';
            }"""
        )

    async def scroll_next(self):
        await self._ensure_started()
        previous_sequence = await self.current_extension_sequence()
        action_started_at = monotonic()
        await self._page.mouse.wheel(0, 750)
        await self._page.wait_for_timeout(750)
        return {
            "success": True,
            "previous_extension_sequence": previous_sequence,
            "action_started_at": action_started_at,
        }

    def is_alive(self):
        """True when Chrome is still attached and the page is still open.

        Read-only: unlike `_ensure_started` it never reconnects, so a health
        check cannot resurrect a browser the operator deliberately closed.
        """
        return bool(
            self._browser
            and self._browser.is_connected()
            and self._page
            and not self._page.is_closed()
        )

    async def _ensure_started(self):
        if not self.is_alive():
            await self.start()

    async def _has_visible_authenticated_dom(self):
        for selector in (
            '[data-e2e="nav-profile"]', '[data-e2e="top-dm-icon"]', '[data-e2e="upload-icon"]',
            '[data-e2e="dm-new-conversation-list"]',
            '[data-e2e="dm-new-conversation-item"]',
            '[data-e2e="dm-new-chatbox"]',
            'a[href="/messages"]', 'a[href^="/@"]',
        ):
            locator = self._page.locator(selector).first
            if await locator.count() and await locator.is_visible():
                return True
        return False

    async def _wait_for_ui_ready(self, require_messages=False):
        """Wait for a stable rendered viewport before snapshot verification/vision."""
        deadline_steps = max(4, min(24, self.timeout_ms // 250))
        stable_signature = None
        stable_count = 0
        transient_errors = 0
        for attempt in range(deadline_steps):
            try:
                state = await self._render_state()
            except Exception as exc:
                if not self._is_transient_navigation_error(exc):
                    raise
                transient_errors += 1
                stable_signature = None
                stable_count = 0
                logger.info(
                    "[DOM] Navigation in progress - retry render state "
                    "attempt=%s/%s error_type=%s",
                    attempt + 1, deadline_steps, type(exc).__name__,
                )
                await self._page.wait_for_timeout(250)
                continue
            messages_ready = not require_messages or state["messages_ready"]
            if state["ready"] and messages_ready:
                signature = (
                    state["visible_count"], state["body_length"], state["messages_ready"]
                )
                if signature == stable_signature:
                    stable_count += 1
                else:
                    stable_signature = signature
                    stable_count = 1
                if stable_count >= 2:
                    return state
            else:
                stable_signature = None
                stable_count = 0
            await self._page.wait_for_timeout(250)
        raise RuntimeError(
            "TikTok page remained blank, loading, or navigating; "
            f"refusing screenshot/verification (transient_navigation_errors={transient_errors})"
        )

    async def _wait_for_fresh_extension_state(
        self,
        previous_sequence: int,
        expected_screen: str | None = None,
        expected_events=None,
        timeout: float = 1.2,
    ):
        """Wait for an extension payload causally newer than a browser action."""
        previous_sequence = max(0, int(previous_sequence or 0))
        wanted_state = normalize_ui_state(expected_screen)
        event_candidates = (
            (expected_events,) if isinstance(expected_events, str)
            else (expected_events or ())
        )
        # Callers name events in their own vocabulary; the extension has one
        # vocabulary, so they are translated here rather than at each call site.
        event_aliases = {
            "composer_changed": {"STATE_CHANGED", "ACTION_RESULT", "PAGE_CHANGED"},
            "message_new": {"NEW_MESSAGE", "MESSAGE_SENT", "STATE_CHANGED"},
            "message_sent": {"MESSAGE_SENT", "STATE_CHANGED"},
            "conversation_list_changed": {"CONVERSATION_LIST_CHANGED", "MESSAGES_PANEL_OPENED"},
            "page_changed": {"PAGE_CHANGED"},
        }
        wanted_events = set()
        for event in event_candidates:
            name = str(event).strip().casefold()
            if not name:
                continue
            wanted_events.update(event_aliases.get(name, {name.upper()}))
        logger.info(
            "[EXT] Waiting for fresh state previous_sequence=%s",
            previous_sequence,
        )
        deadline = monotonic() + max(0.0, float(timeout))
        highest_sequence_seen = previous_sequence
        while monotonic() < deadline:
            state = await self._read_extension_state()
            if state is not None:
                sequence = int(state.get("sequence", 0) or 0)
                highest_sequence_seen = max(highest_sequence_seen, sequence)
                observed_state = str(state.get("ui_state", "UNKNOWN")).strip().upper()
                event_types = {
                    str(event).strip().upper() for event in (state.get("events") or [])
                }
                if (
                    sequence > previous_sequence
                    and (wanted_state is None or observed_state == wanted_state)
                    # Events are corroborating evidence only. A payload that
                    # reports none must not block a genuinely newer sequence.
                    and (
                        not wanted_events
                        or not event_types
                        or bool(wanted_events.intersection(event_types))
                    )
                ):
                    self._extension_state_floor = 0
                    self._force_dom_snapshot_once = False
                    self._perception_wait_result = True
                    logger.info(
                        "[EXT] Fresh state received seq=%s state=%s mode=%s",
                        sequence,
                        observed_state,
                        state.get("messages_mode") or "none",
                    )
                    return state
            await asyncio.sleep(0.05)
        # snapshot() must not consume the same pre-action payload after this
        # timeout. Its normal browser DOM collection remains available.
        self._extension_state_floor = highest_sequence_seen
        self._force_dom_snapshot_once = True
        self._perception_wait_result = False
        logger.info("[EXT] Fresh state timeout - fallback to browser DOM")
        return None

    async def _current_extension_sequence(self):
        state = await self._read_extension_state()
        if state is not None:
            return int(state.get("sequence", 0) or 0)
        return self._last_extension_sequence

    @classmethod
    def _is_transient_navigation_error(cls, exc):
        message = str(exc).casefold()
        return any(marker in message for marker in cls._TRANSIENT_NAVIGATION_ERRORS)

    async def _render_state(self):
        return await self._page.evaluate(
            """() => {
                const body = document.body;
                const text = (body?.innerText || '').trim();
                const normalized = text.toLowerCase();
                const loadingOnly = normalized === 'loading...' || normalized === 'loading';
                const candidates = body ? body.querySelectorAll(
                    'a,button,input,textarea,[role="button"],[role="textbox"],'
                    + '[data-e2e="dm-new-conversation-list"],'
                    + '[data-e2e="dm-new-conversation-item"],'
                    + '[data-e2e="dm-new-chatbox"]'
                ) : [];
                let visibleCount = 0;
                for (const node of candidates) {
                    const rect = node.getBoundingClientRect();
                    const style = getComputedStyle(node);
                    if (rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden'
                        && style.display !== 'none') visibleCount++;
                }
                const messagesReady = !!document.querySelector(
                    '[data-e2e="dm-new-conversation-list"],'
                    + '[data-e2e="dm-new-conversation-item"],'
                    + '[data-e2e="dm-new-chatbox"]'
                );
                const documentReady = document.readyState === 'interactive'
                    || document.readyState === 'complete';
                const loading = !documentReady || loadingOnly
                    || (text.length === 0 && visibleCount < 2);
                return {
                    ready: documentReady && !loading && (text.length > 0 || visibleCount >= 3),
                    loading,
                    body_length: text.length,
                    visible_count: visibleCount,
                    messages_ready: messagesReady,
                };
            }"""
        )

    async def _has_visible_login_form(self):
        for selector in (
            'input[type="password"]', 'input[name*="username" i]',
            'input[autocomplete="username"]', '[data-e2e*="login" i] input',
        ):
            locator = self._page.locator(selector).first
            if await locator.count() and await locator.is_visible():
                return True
        return False

    def _is_tiktok_url(self, url):
        parsed = urlparse(url)
        return bool(parsed.hostname) and (
            parsed.hostname == self._allowed_host or parsed.hostname.endswith(f".{self._allowed_host}")
        )

    def _validate_url(self, url):
        parsed = urlparse(url)
        if parsed.scheme != "https" or not self._is_tiktok_url(url):
            raise ValueError("Browser navigation is restricted to the configured TikTok origin")

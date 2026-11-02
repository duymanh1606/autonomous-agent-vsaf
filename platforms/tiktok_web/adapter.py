import json
import logging

from models.action import Action
from models.auth import AuthState
from models.ui_errors import TargetNotFound
from platforms.base import PlatformAdapter
from platforms.tiktok.screens import TikTokScreen
from platforms.tiktok_web.selectors import CHAT_HEADER_RESOURCE_IDS, SELECTORS


# TikTok draws the inbox as a drawer over the feed, so a conversation opened
# inside that drawer is still "the Messages surface". Every step of the
# messaging flow accepts either screen.
MESSAGING_SCREENS = frozenset({TikTokScreen.MESSAGES, TikTokScreen.CONVERSATION})


# Capabilities the planner resolves in code, to the one action that gets
# there. Anything outside these and the skills' own capabilities would need
# the planner's LLM, so this is also (with them) the list of what a caller
# may submit at all - see main._submittable_capabilities.
DETERMINISTIC_ROUTES = {
    "open_tiktok": "browser.open_tiktok",
    "open_inbox": "platform.open_inbox",
    "check_unread_messages": "platform.open_inbox",
    "open_profile": "platform.open_profile",
    "search_content": "platform.open_search",
    "open_comments": "platform.open_comments",
    "check_notifications": "platform.open_notifications",
    "prepare_video_upload": "platform.open_upload",
    "browse_feed": "platform.open_home",
    "inspect_video": "platform.open_home",
}

CAPABILITY_TARGETS = {
    "open_tiktok": {TikTokScreen.VIDEO_FEED},
    "open_inbox": set(MESSAGING_SCREENS),
    "check_unread_messages": set(MESSAGING_SCREENS),
    "open_profile": {TikTokScreen.PROFILE},
    "search_content": {TikTokScreen.SEARCH_RESULTS},
    "browse_feed": {TikTokScreen.VIDEO_FEED},
    "inspect_video": {TikTokScreen.VIDEO_FEED},
    "open_comments": {TikTokScreen.COMMENTS},
    "check_notifications": {TikTokScreen.NOTIFICATIONS},
    "prepare_video_upload": {TikTokScreen.UPLOAD, TikTokScreen.UPLOAD_READY},
}


logger = logging.getLogger(__name__)


class TikTokWebAdapter(PlatformAdapter):
    name = "tiktok_web"

    def __init__(
        self, browser, observer, base_url="https://www.tiktok.com",
        dom_resolver=None, action_executor=None, state_verifier=None,
        dom_observer=None,
    ):
        self.browser = browser
        self.observer = observer
        self.base_url = base_url.rstrip("/")
        self.dom_resolver = dom_resolver
        self.action_executor = action_executor
        self.state_verifier = state_verifier
        self.dom_observer = dom_observer

    def register_tools(self, registry):
        handlers = {
            "platform.open_home": lambda: self.browser.click_first(SELECTORS["home"]),
            "platform.detect_unread": self.detect_unread,
            "platform.open_inbox": self.open_inbox,
            "platform.open_profile": lambda: self.browser.click_first(SELECTORS["profile"]),
            "platform.open_search": self.open_search,
            "platform.open_upload": lambda: self.browser.click_first(SELECTORS["upload"]),
            "platform.open_comments": lambda: self.browser.click_first(SELECTORS["comments"]),
            "platform.open_notifications": lambda: self.browser.click_first(SELECTORS["notifications"]),
            "platform.open_conversation": self.open_conversation,
            "platform.open_unread_conversation": self.open_unread_conversation,
            "platform.list_conversations": self.list_conversations,
            "platform.list_unread_conversations": self.list_unread_conversations,
            "platform.list_all_conversations": self.list_all_conversations,
            "platform.reload_page": lambda: self.browser.reload_page(),
            "platform.detect_message_requests": self.detect_message_requests,
            "platform.open_message_requests": self.open_message_requests,
            "platform.list_message_requests": self.list_message_requests,
            "platform.open_message_request": self.open_message_request,
            "platform.accept_message_request": self.accept_message_request,
            "platform.return_to_primary_inbox": self.return_to_primary_inbox,
            "platform.focus_message_input": self.focus_message_input,
            "platform.attach_message_media": self.attach_message_media,
            "platform.clear_message_draft": self.clear_message_draft,
            "platform.send_message": self.send_message,
            "platform.retry_send": self.send_message,
            "platform.swipe_next": self.browser.scroll_next,
            "browser.input_text": self.input_text,
        }
        for name, handler in handlers.items():
            registry.register(name, handler)

    async def detect_unread(self):
        """Read-only unread check used before leaving the current screen.

        Only the Messages entry point counts. The Activity/notification badge
        is reported separately and never contributes to `unread_count`.
        """
        if self.dom_observer is None:
            return {"success": True, "source": "dom", "unread": False, "conversations": []}
        state = await self.dom_observer.observe_state()
        conversations = list(state.unread_conversations)
        logger.info(
            "[INBOX] messages badge present=%s count=%s activity_badge=%s "
            "screen=%s mode=%s rows=%s unread_rows=%s",
            state.unread_badge, state.unread_count, state.activity_unread_count,
            state.screen, state.messages_mode or "none",
            len(state.conversations), len(conversations),
        )
        return {
            "success": True,
            "source": state.source,
            "screen": state.screen,
            "messages_mode": state.messages_mode,
            "messages_panel_visible": state.messages_panel_visible,
            "unread": bool(state.has_unread),
            "unread_count": state.unread_count,
            "activity_unread_count": state.activity_unread_count,
            "conversations": conversations,
            "conversations_observed": len(state.conversations),
        }

    async def _click_and_verify(self, target, expected_screen, verify=None):
        """Click a resolved DOM target, then confirm the state by re-reading DOM.

        `verify` receives the extension sequence read immediately before the
        click, so post-action checks can require a strictly newer state instead
        of trusting the URL.
        """
        result = await self.action_executor.click(target, expected_screen=expected_screen)
        if self.state_verifier is None:
            return result
        previous_sequence = result.get("previous_extension_sequence")
        if verify is None:
            confirmed, state = await self.state_verifier.confirm_screen(expected_screen)
        else:
            confirmed, state = await verify(previous_sequence)
        result = {
            **result,
            "verified_state": bool(confirmed),
            "observed_screen": getattr(state, "screen", None),
        }
        if confirmed:
            return result
        # The click was issued but the UI did not reach the expected state.
        # Do not escalate to coordinate/vision here: that would click twice.
        return {
            **result,
            "success": False,
            "error": (
                f"State verification failed: expected {expected_screen!r}, "
                f"observed {getattr(state, 'screen', None)!r}"
            ),
        }

    async def open_inbox(self):
        """Open the Messages surface by clicking the nav entry, never by URL.

        TikTok normally answers with an inbox panel drawn over the feed rather
        than a full Messages page; both satisfy this step.
        """
        if self.dom_observer is not None:
            state = await self.dom_observer.observe_state()
            if self._messages_surface_open(state):
                # Idempotent: MESSAGES or a CONVERSATION opened inside the
                # panel already satisfies "the Messages surface is open", so
                # this is a success and never a second click.
                logger.info(
                    "[INBOX] messages already open screen=%s mode=%s conversations=%s",
                    state.screen, state.messages_mode or "none",
                    len(state.conversations),
                )
                return {
                    "success": True, "source": state.source, "target": "inbox_button",
                    "already_open": True, "messages_mode": state.messages_mode,
                    "screen": state.screen,
                }
        if self.dom_resolver and self.action_executor:
            target = await self.dom_resolver.resolve_target("messages_button")
            if target is None:
                raise TargetNotFound("inbox_button")
            logger.info("[ACTION] click role=messages_nav selector=%s", target.selector)
            return await self._click_and_verify(
                target, "messages", verify=self._verify_messages_open
            )
        return await self.browser.click_ui_target(
            SELECTORS["inbox"],
            target_name="inbox_button",
            expected_screen="messages",
        )

    @staticmethod
    def _messages_surface_open(state):
        """True for the inbox drawer, the Messages page and a panel conversation."""
        return bool(
            state.messages_open
            or state.screen in MESSAGING_SCREENS
            and (state.messages_mode == "panel" or state.messages_panel_present)
        )

    async def _verify_messages_open(self, previous_sequence=None):
        """MESSAGES is confirmed by a newer state plus the panel, not by the URL.

        The expected state is MESSAGES *or* CONVERSATION: clicking the nav entry
        while a conversation is already open inside the drawer leaves the
        conversation on screen, and that is a success, not a failure.
        """
        if self.dom_observer is None:
            return await self.state_verifier.confirm_ui_state(
                MESSAGING_SCREENS, previous_sequence=previous_sequence
            )
        barrier = None if previous_sequence is None else int(previous_sequence or 0)

        def opened(observed):
            if not self._messages_surface_open(observed):
                return False
            # A sequence that did not move means the extension has not seen the
            # click yet; keep polling. Only `>` is required, never `previous + 1`.
            if barrier is None or not observed.sequence:
                return True
            return observed.is_newer_than(barrier)

        state = await self.dom_observer.wait_for(opened, reason="messages_open")
        confirmed = opened(state)
        logger.info(
            "[VERIFY] messages open=%s screen=%s mode=%s sequence=%s previous=%s "
            "conversations=%s panel_visible=%s",
            confirmed, state.screen, state.messages_mode or "none",
            state.sequence, "none" if barrier is None else barrier,
            len(state.conversations), state.messages_panel_visible,
        )
        return confirmed, state

    async def open_search(self, query=None):
        if not query:
            raise ValueError("search_content requires a non-empty query")
        if len(query) > 200:
            raise ValueError("Search query is too long")
        navigation = await self.browser.click_first(SELECTORS["search"])
        await self.browser.fill_and_submit(SELECTORS["search_input"], query)
        return {**navigation, "success": True, "query": query, "target": "search"}

    async def _row_selector(self, target, unread_only=False):
        """The selector the extension recorded for this exact row, if any.

        Action priority is selector -> semantic role -> rect centre -> vision;
        this method supplies the first step so a click never falls back to a
        coordinate while a usable DOM handle exists.
        """
        if self.dom_observer is None:
            return None
        wanted = str(target or "").strip().casefold().lstrip("@")
        if not wanted:
            return None
        state = await self.dom_observer.observe_state()
        for row in state.conversations:
            if unread_only and not row.unread:
                continue
            if row.name.strip().casefold().lstrip("@") == wanted and row.selector:
                return row.selector
        return None

    async def open_conversation(self, target):
        if self.dom_resolver and self.action_executor:
            selector = await self._row_selector(target)
            resolved = await self.dom_resolver.resolve_target(
                "conversation", target_text=target.strip(), selector=selector
            )
            if resolved is None:
                raise TargetNotFound("conversation_item")
            logger.info(
                "[ACTION] open conversation username=%s selector=%s",
                target, resolved.selector,
            )
            return await self._click_and_verify(
                resolved, "conversation",
                verify=lambda previous: self.state_verifier.confirm_conversation(
                    target, previous_sequence=previous
                ),
            )
        return await self.browser.open_conversation(target.strip(), SELECTORS)

    async def open_unread_conversation(self, target):
        if self.dom_resolver and self.action_executor:
            selector = await self._row_selector(target, unread_only=True)
            resolved = await self.dom_resolver.resolve_target(
                "unread_conversation",
                target_text=target.strip(),
                unread_only=True,
                selector=selector,
            )
            if resolved is None:
                raise TargetNotFound("unread_conversation")
            logger.info(
                "[ACTION] open conversation username=%s selector=%s unread=True",
                target, resolved.selector,
            )
            if self.state_verifier is None:
                return await self.action_executor.click(
                    resolved, expected_screen="conversation"
                )
            return await self._click_and_verify(
                resolved, "conversation",
                verify=lambda previous: self.state_verifier.confirm_conversation(
                    target, previous_sequence=previous
                ),
            )
        return await self.browser.open_unread_conversation(target.strip(), SELECTORS)

    async def list_conversations(self, limit):
        """List rows the extension scoped to the Messages panel/page."""
        if self.dom_observer is not None:
            state = await self.dom_observer.observe_state()
            if state.conversations:
                shown = state.conversations[: max(1, int(limit))]
                return {
                    "success": True,
                    "source": state.source,
                    "conversations": [row.name for row in shown],
                    # What each row shows without opening it - the inbox scan
                    # compares the preview with what it last synced to notice
                    # a conversation read (or answered) on another device,
                    # which never carries TikTok's unread marker here.
                    "rows": [
                        {
                            "name": row.name, "preview": row.preview,
                            "unread": row.unread, "message_request": row.message_request,
                        }
                        for row in shown
                    ],
                }
        return await self.browser.list_conversations(limit, SELECTORS)

    async def list_unread_conversations(self, limit):
        if self.dom_observer is not None:
            state = await self.dom_observer.observe_state()
            # An unaccepted message request can render inline in this same
            # panel, flagged unread. Its thread shows only pre-accept system
            # banners until Accept is clicked, so opening it here would never
            # find a real inbound message - it belongs exclusively to the
            # dedicated request queue (`platform.list_message_requests` /
            # `_handle_message_requests`), which is the only place that can
            # actually unlock it.
            unread = [
                row for row in state.conversations
                if row.unread and not row.message_request
            ]
            logger.info(
                "[INBOX] unread conversations=%s of rows=%s mode=%s",
                len(unread), len(state.conversations), state.messages_mode or "none",
            )
            if unread:
                return {
                    "success": True,
                    "source": state.source,
                    "conversations": [
                        row.name for row in unread[: max(1, int(limit))]
                    ],
                }
            if state.conversations:
                if state.unread_count > 0:
                    logger.info(
                        "[INBOX] unread badge=%s but no rendered row is marked unread; "
                        "scanning the virtualized conversation list",
                        state.unread_count,
                    )
                    return await self.browser.list_unread_conversations(limit, SELECTORS)
                # The panel is open and was scanned: an empty result is a real
                # answer, not a missing signal.
                return {"success": True, "source": state.source, "conversations": []}
        if self.dom_resolver is not None:
            targets = await self.dom_resolver.resolve_unread_conversations(limit)
            if targets:
                return {
                    "success": True,
                    "source": "dom",
                    "conversations": [target.text for target in targets],
                }
        return await self.browser.list_unread_conversations(limit, SELECTORS)

    async def list_all_conversations(self, limit):
        """Every visible conversation, scrolled to the end - not just unread.

        Deliberately skips the `dom_observer` fast path `list_conversations`/
        `list_unread_conversations` use above: that path only ever returns a
        snapshot of the currently rendered rows, with no scrolling. The
        one-time historical backfill this powers needs the real full list, so
        it always pays for the slower, scrolling Playwright read.
        """
        return await self.browser.list_all_conversations(limit, SELECTORS)

    async def detect_message_requests(self):
        return await self.browser.detect_message_requests(SELECTORS)

    async def open_message_requests(self):
        return await self.browser.open_message_requests(SELECTORS)

    async def list_message_requests(self, limit):
        return await self.browser.list_message_requests(limit, SELECTORS)

    async def open_message_request(self, target):
        return await self.browser.open_message_request(target.strip(), SELECTORS)

    async def accept_message_request(self, target):
        return await self.browser.accept_message_request(target.strip(), SELECTORS)

    async def return_to_primary_inbox(self):
        return await self.browser.return_to_primary_inbox(SELECTORS)

    @staticmethod
    def _normalized_recipient(value):
        return str(value or "").strip().casefold().lstrip("@")

    async def _require_active_recipient(self, recipient):
        """Fail closed unless a fresh DOM header names the requested recipient.

        A single read is not enough to conclude anything against the thread:
        the extension bridge read has a 500ms budget (`_read_extension_state`
        in tools/browser.py) and a page busy staging an attachment can miss
        it. The resulting raw-DOM fallback state carries no header username at
        all, which used to be reported as "active chat does not match
        recipient" - a wrong-thread verdict about a thread that was in fact
        the right one. So a failed first read is re-read through the verifier
        before anything is decided. Fail-closed is unchanged: a header that
        never names the recipient still blocks the action, only now the reason
        says which of the two it was.
        """
        wanted = self._normalized_recipient(recipient)
        if not wanted:
            raise RuntimeError("recipient_mismatch: recipient is required")
        reader = getattr(self.state_verifier, "read_conversation", None)
        if reader is None:
            raise RuntimeError("recipient_mismatch: fresh DOM reader is unavailable")
        state = await reader()
        if self._recipient_verified(state, wanted):
            return state
        confirm = getattr(self.state_verifier, "confirm_conversation", None)
        if confirm is not None and not self._header_named(state):
            # Only an unreadable header is re-read. A header that names
            # somebody else is real evidence and aborts immediately, exactly
            # as before: polling it would mean sending to whoever the DOM
            # happens to show at the end of the budget.
            confirmed, polled = await confirm(recipient)
            if confirmed:
                return polled
            if self._header_named(polled):
                state = polled
        raise RuntimeError(
            f"recipient_mismatch: {self._recipient_failure(state, wanted)}"
        )

    @classmethod
    def _recipient_verified(cls, state, wanted):
        return bool(
            state is not None
            and getattr(state, "active_chat_present", False)
            and cls._normalized_recipient(getattr(state, "active_chat", "")) == wanted
            and getattr(state, "composer_present", False)
        )

    @classmethod
    def _header_named(cls, state):
        """True when this state's header names somebody - anybody."""
        return bool(
            state is not None
            and getattr(state, "active_chat_present", False)
            and cls._normalized_recipient(getattr(state, "active_chat", ""))
        )

    @classmethod
    def _recipient_failure(cls, state, wanted):
        """Why the gate refused, with "unreadable" kept apart from "wrong thread"."""
        if state is None:
            return f"the open thread could not be read at all (wanted={wanted})"
        active = cls._normalized_recipient(getattr(state, "active_chat", ""))
        if not getattr(state, "active_chat_present", False) or not active:
            return (
                "the conversation header did not render "
                f"(source={getattr(state, 'source', None) or 'unknown'} "
                f"wanted={wanted}); the open thread is unverified, "
                "not known to be wrong"
            )
        if active != wanted:
            return (
                "active chat does not match recipient "
                f"(active={active} wanted={wanted})"
            )
        return "recipient composer is unavailable"

    async def _require_no_stale_media(self):
        """Refuse to type while the composer still holds unsent media.

        Leftover text is harmless - `fill()` replaces it - but a staged file
        is not: nothing in the send path removes an attachment the agent did
        not put there, so it would ride along with the next reply. This is how
        a draft gets left behind: an aborted reply calls
        `platform.clear_message_draft`, and that clear can itself fail (it did
        on ws-bc391da5 on 2026-09-21, when the extension bridge read timed
        out mid-upload), leaving text plus a staged image in the composer.

        Read-only: `inspect_message_draft` never clicks, fills or navigates.
        Raising here fails the typing action, and `ReplyMessageSkill` answers
        that by clearing the draft and aborting before send - so the next scan
        retries this customer on a clean composer.
        """
        draft = await self.browser.inspect_message_draft(
            SELECTORS["message_input"],
            SELECTORS["message_media_input"],
            # Draft-only selectors: the broad preview set also matches images
            # already sent in the thread.
            SELECTORS["message_media_draft_preview"],
            SELECTORS["message_media_dialog"],
        )
        if draft.get("media_present"):
            raise RuntimeError(
                "stale_draft: the composer still holds unsent media from an "
                f"earlier attempt (file_count={draft.get('file_count')} "
                f"preview_visible={draft.get('preview_visible')} "
                f"dialog_visible={draft.get('dialog_visible')}); "
                "it has to be cleared before a reply is typed"
            )
        return draft

    async def _require_clean_composer_for(self, recipient):
        """The full pre-typing gate: right thread, no unsent media in it."""
        state = await self._require_active_recipient(recipient)
        await self._require_no_stale_media()
        return state

    async def input_text(self, text, visual_target_id=None, recipient=None):
        gate = None
        if recipient is not None:
            gate = lambda: self._require_clean_composer_for(recipient)
        result = await self.browser.input_message(
            text, SELECTORS["message_input"], visual_target_id=visual_target_id,
            pre_input=gate,
        )
        if self.state_verifier is None:
            return result
        # Annotate only: the graph already verifies composer text through the
        # observation, and a stricter gate here would double-fail on composers
        # whose text the flat DOM snapshot cannot read exactly.
        confirmed, _ = await self.state_verifier.confirm_composer_text(text)
        return {**result, "verified_state": bool(confirmed)}

    async def focus_message_input(self, recipient=None):
        if recipient is not None:
            await self._require_active_recipient(recipient)
        if self.dom_resolver and self.action_executor:
            target = await self.dom_resolver.resolve_target("message_input")
            if target is None:
                raise TargetNotFound("message_input")
            return await self._click_and_verify(target, "conversation")
        return await self.browser.click_first(SELECTORS["message_input"])

    async def attach_message_media(self, path, recipient=None):
        gate = None
        if recipient is not None:
            await self._require_active_recipient(recipient)
            gate = lambda: self._require_active_recipient(recipient)
        return await self.browser.attach_message_media(
            path,
            SELECTORS["message_media_input"],
            SELECTORS["message_media_preview"],
            SELECTORS["message_media_uploading"],
            SELECTORS["message_media_error"],
            SELECTORS["message_media_button"],
            SELECTORS["message_media_confirm"],
            SELECTORS["message_media_dialog"],
            recipient_gate=gate,
        )

    async def clear_message_draft(self):
        return await self.browser.clear_message_draft(
            SELECTORS["message_input"],
            SELECTORS["message_media_input"],
            SELECTORS["message_media_draft_preview"],
            SELECTORS["message_media_dialog"],
            SELECTORS["message_media_cancel"],
            SELECTORS["message_media_remove"],
        )

    async def send_message(
        self, text=None, visual_target_id=None, conversation_id=None,
        message_signature=None, recipient=None,
    ):
        gate = None
        if recipient is not None:
            await self._require_active_recipient(recipient)
            gate = lambda: self._require_active_recipient(recipient)
        send_target = None
        if self.dom_resolver:
            send_target = await self.dom_resolver.resolve_target("send_button")
            if send_target is None:
                raise TargetNotFound("send_button")
        result = await self.browser.send_message(
            text, SELECTORS["message_input"], SELECTORS["message_text"],
            visual_target_id=visual_target_id,
            send_target=send_target,
            action_executor=self.action_executor,
            pre_send=gate,
        )
        if self.state_verifier is None or not text:
            return result
        # Send is attempted exactly once. Verification only annotates the
        # evidence; classification into SENT/NOT_SENT/UNCERTAIN and any retry
        # decision stay with the observer, policy and messaging skill.
        confirmed, _ = await self.state_verifier.confirm_message_sent(text)
        return {**result, "verified_state": bool(confirmed)}

    def next_action(self, task, observation):
        if observation.auth_state != AuthState.LOGGED_IN:
            return Action(name="abort_task", args={
                "reason": f"Authentication is not confirmed: {observation.auth_state.value}"
            })
        if self.task_complete(task, observation):
            return None
        if observation.screen == TikTokScreen.DIALOG:
            return Action(name="abort_task", args={"reason": "Manual TikTok login is required in Chrome"})
        if observation.unknown:
            return Action(name="browser.open_tiktok", reason="Establish a known TikTok Web state")
        if task.capability == "search_content" and not str(task.payload.get("query", "")).strip():
            return Action(name="abort_task", args={"reason": "search_content requires a non-empty query"})

        name = DETERMINISTIC_ROUTES.get(task.capability)
        if not name:
            return None
        args = (
            {"query": task.payload.get("query")}
            if task.capability == "search_content" else {}
        )
        return Action(name=name, args=args, reason=f"Deterministic web route for {task.capability}")

    def task_complete(self, task, observation):
        return (
            observation.auth_state == AuthState.LOGGED_IN
            and observation.screen in CAPABILITY_TARGETS.get(task.capability, set())
        )

    def verify(self, action, before_observation, observation):
        if observation.security_challenge:
            return False
        if action.name == "browser.check_auth":
            return observation.auth_state == AuthState.LOGGED_IN
        if action.name not in {"browser.snapshot", "browser.screenshot"}:
            if observation.auth_state != AuthState.LOGGED_IN:
                return False
        expected = {
            "browser.open_tiktok": TikTokScreen.VIDEO_FEED,
            "platform.open_home": TikTokScreen.VIDEO_FEED,
            "platform.open_inbox": TikTokScreen.INBOX,
            "platform.open_profile": TikTokScreen.PROFILE,
            "platform.open_search": TikTokScreen.SEARCH_RESULTS,
            "platform.open_conversation": TikTokScreen.CONVERSATION,
            "platform.open_unread_conversation": TikTokScreen.CONVERSATION,
        }.get(action.name)
        if expected:
            if action.name == "platform.open_inbox":
                # The Messages surface is the drawer or the page; a conversation
                # already open inside the drawer counts as reached, so a
                # no-op open_inbox verifies instead of failing the task.
                return observation.screen in MESSAGING_SCREENS
            if action.name in {
                "platform.open_conversation", "platform.open_unread_conversation"
            }:
                return (
                    observation.screen == TikTokScreen.CONVERSATION
                    and observation.auth_state == AuthState.LOGGED_IN
                    and self._conversation_matches(
                        observation, action.args.get("target")
                    )
                )
            return observation.screen == expected
        if action.name in {"browser.snapshot", "browser.screenshot"}:
            return observation is not None
        if action.name == "browser.back":
            return not observation.unknown
        if action.name == "browser.click_bbox":
            target_id = action.args.get("target_id")
            target = next((
                item for item in (before_observation.visual_targets if before_observation else [])
                if item.target_id == target_id
            ), None)
            if not target:
                return False
            if target.type == "inbox_button":
                return observation.screen == TikTokScreen.MESSAGES
            if target.type == "conversation":
                return observation.screen == TikTokScreen.CONVERSATION
            if target.type in {"conversation_item", "unread_conversation"}:
                return observation.screen == TikTokScreen.CONVERSATION
            if target.type in {"message_input", "send_button"}:
                return observation.screen == TikTokScreen.CONVERSATION
            if target.type == "back_button":
                return not observation.unknown
            return False
        if action.name == "platform.focus_message_input":
            return (
                observation.screen == TikTokScreen.CONVERSATION
                and self._conversation_matches(observation, action.args.get("recipient"))
            )
        if action.name == "platform.clear_message_draft":
            return (
                observation.screen == TikTokScreen.CONVERSATION
                and observation.composer_state.value == "EMPTY"
            )
        if action.name == "browser.input_text":
            wanted = action.args.get("text", "").strip().casefold()
            return observation.screen == TikTokScreen.CONVERSATION and bool(wanted) and (
                observation.composer_text_matches_expected is True
                or any(
                    wanted == str(element.get("text", "")).strip().casefold()
                    for element in observation.elements
                    if element.get("role") in {"textarea", "textbox"}
                )
            )
        if action.name in {"platform.send_message", "platform.retry_send"}:
            if (
                observation.screen == TikTokScreen.CONVERSATION
                and observation.message_send_state.value == "SENT"
            ):
                return True
            # Compatibility for observations created outside the new
            # verification observer. Runtime observations always set the
            # last_message_visible field to True/False for send actions.
            wanted = action.args.get("text", "").strip().casefold()
            return observation.last_message_visible is None and bool(wanted) and any(
                str(element.get("resource_id", "")) == "dm-new-message-text"
                and wanted == str(element.get("text", "")).strip().casefold()
                for element in observation.elements
            )
        if action.name == "platform.swipe_next":
            before = self._signature(before_observation)
            after = self._signature(observation)
            return observation.screen == TikTokScreen.VIDEO_FEED and bool(before) and before != after
        if action.name in {"platform.open_comments", "platform.open_notifications"}:
            return not observation.unknown
        if action.name == "platform.open_upload":
            return "/upload" in observation.metadata.get("url", "") or observation.screen in {
                TikTokScreen.UPLOAD, TikTokScreen.UPLOAD_READY,
            }
        return not observation.unknown

    @staticmethod
    def _signature(observation):
        if not observation:
            return None
        if observation.metadata.get("signature"):
            return observation.metadata["signature"]
        try:
            return json.loads(observation.raw_ui_xml or "{}").get("signature")
        except json.JSONDecodeError:
            return observation.raw_ui_xml

    @staticmethod
    def _conversation_matches(observation, target):
        """DOM evidence that the open conversation belongs to `target`.

        The extension's `active_chat` is preferred; the flat element scan stays
        as the fallback. The URL is never consulted.
        """
        normalized = str(target or "").strip().casefold().lstrip("@")
        if not normalized:
            return False
        extension_state = (observation.metadata or {}).get("extension_state") or {}
        active_chat = extension_state.get("active_chat") or {}
        username = str(active_chat.get("username") or "").strip().casefold().lstrip("@")
        if active_chat.get("present") and username:
            # Exact only. A substring match let `manh1` verify as the open
            # conversation of `manh14760`, so a reply could be confirmed
            # against the wrong recipient.
            return username == normalized
        return any(
            (
                str(element.get("role", "")).casefold() == "chat_header"
                or str(element.get("resource_id", "")).casefold()
                in CHAT_HEADER_RESOURCE_IDS
            )
            and str(element.get("text", "")).strip().casefold().lstrip("@")
            == normalized
            for element in observation.elements
        )

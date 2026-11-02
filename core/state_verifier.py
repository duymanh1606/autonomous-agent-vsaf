import logging
from time import monotonic

from platforms.tiktok.screens import TikTokScreen
from platforms.tiktok_web.ui_states import SHORT_NAME_TO_UI_STATE, screen_for_ui_state


logger = logging.getLogger(__name__)

# The longest a thread read is reused when the extension's sequence has not
# moved. Short: it only has to span the handful of checks that guard one
# action, and it is the backstop for an extension that stopped publishing
# (where an unchanged sequence means "no news", not "nothing changed").
MAX_REUSE_SECONDS = 1.0


# Executors and the coordinate config describe screens with short names; they
# resolve through the one mapping in platforms.tiktok_web.ui_states so no layer
# keeps a private table.
SCREEN_ALIASES = {
    name: screen_for_ui_state(ui_state)
    for name, ui_state in SHORT_NAME_TO_UI_STATE.items()
}


class StateVerifier:
    """Verify post-action state by re-reading the DOM, never by sleeping.

    `verify()` keeps the synchronous contract used by the LangGraph action
    loop. The async `confirm_*` helpers are the DOM-state gate used directly
    by the TikTok Web flow after every click, input and send.
    """

    def __init__(self, platform, dom_observer=None, timeout_ms=4000):
        self.platform = platform
        self.dom_observer = dom_observer
        self.timeout_ms = max(0, int(timeout_ms))
        # The last thread read, reused while the page has not published a new
        # state - see `read_conversation`.
        self._last_read = None

    def verify(self, action, before_observation, after_observation):
        return bool(
            after_observation is not None
            and self.platform.verify(action, before_observation, after_observation)
        )

    @staticmethod
    def canonical_screen(expected_screen):
        if not expected_screen:
            return None
        value = str(expected_screen).strip()
        return SCREEN_ALIASES.get(value.casefold(), value)

    async def confirm_screen(self, expected_screen, timeout_ms=None):
        """Poll the DOM until the expected screen is rendered."""
        expected = self.canonical_screen(expected_screen)
        if self.dom_observer is None or expected is None:
            return True, None
        state = await self.dom_observer.wait_for_screen(
            expected, timeout_ms=self.timeout_ms if timeout_ms is None else timeout_ms
        )
        confirmed = state.is_screen(expected)
        logger.info(
            "[VERIFY] screen expected=%s observed=%s confirmed=%s source=%s",
            expected, state.screen, confirmed, state.source,
        )
        return confirmed, state

    @classmethod
    def canonical_screens(cls, expected_screens):
        """Normalize one screen name or a collection of them into a set."""
        if not expected_screens:
            return set()
        if isinstance(expected_screens, (str, bytes)):
            expected_screens = [expected_screens]
        return {
            screen for screen in (
                cls.canonical_screen(item) for item in expected_screens
            ) if screen
        }

    async def confirm_ui_state(
        self, expected_screens, previous_sequence=None, timeout_ms=None,
        reason="ui_state",
    ):
        """Confirm a UI action from the extension sequence plus the screen.

        The gate is `sequence > previous_sequence` AND the screen being one of
        `expected_screens` (for the messaging flow: MESSAGES or CONVERSATION).
        Only `>` is required - the extension may publish several states for one
        action, so `previous + 1` is never assumed.

        The URL is deliberately never consulted: TikTok opens both the inbox
        drawer and a conversation without changing it. When no extension
        sequence is available (plain DOM snapshot), the screen alone decides,
        because a missing sequence cannot disprove the new state either.
        """
        expected = self.canonical_screens(expected_screens)
        if self.dom_observer is None or not expected:
            return True, None
        barrier = None if previous_sequence is None else int(previous_sequence or 0)

        def reached(observed):
            if not observed.is_any_screen(expected):
                return False
            if barrier is None or not observed.sequence:
                return True
            return observed.is_newer_than(barrier)

        state = await self.dom_observer.wait_for(
            reached,
            timeout_ms=self.timeout_ms if timeout_ms is None else timeout_ms,
            reason=reason,
        )
        confirmed = reached(state)
        logger.info(
            "[VERIFY] ui_state expected=%s observed=%s sequence=%s previous=%s "
            "confirmed=%s source=%s",
            sorted(expected), state.screen, state.sequence,
            "none" if barrier is None else barrier, confirmed, state.source,
        )
        return confirmed, state

    async def confirm_conversation(self, target, previous_sequence=None, timeout_ms=None):
        """Confirm a conversation is open, from DOM evidence only.

        The gate is `active_chat == target AND composer present`, optionally
        behind the same `sequence > previous_sequence` barrier the other
        post-action checks use. The URL is irrelevant: TikTok opens a
        conversation inside the inbox panel without changing it.
        """
        if self.dom_observer is None:
            return False, None
        wanted = str(target or "").strip().casefold().lstrip("@")
        if not wanted:
            return False, None
        barrier = None if previous_sequence is None else int(previous_sequence or 0)

        def opened(observed):
            if barrier is not None and observed.sequence and not observed.is_newer_than(
                barrier
            ):
                return False
            active = str(observed.active_chat or "").strip().casefold().lstrip("@")
            return bool(
                observed.is_screen(TikTokScreen.CONVERSATION)
                and observed.active_chat_present
                and active == wanted
                and observed.composer_present
            )

        state = await self.dom_observer.wait_for(
            opened,
            timeout_ms=self.timeout_ms if timeout_ms is None else timeout_ms,
            reason="conversation_open",
        )
        confirmed = opened(state)
        logger.info(
            "[VERIFY] conversation target=%s screen=%s active_chat=%r "
            "active_chat_present=%s composer=%s confirmed=%s",
            wanted or "unspecified", state.screen, state.active_chat,
            state.active_chat_present, state.composer_present, confirmed,
        )
        return confirmed, state

    async def read_conversation(self, *, force=False):
        """The open thread as the DOM currently shows it, or `None`.

        This is the DOM-truth entry point used before deciding to send and
        after sending; it never clicks and never navigates.

        One reply used to pay for four of these within a couple of hundred
        milliseconds: the messaging skill checked the recipient, then the
        platform adapter checked it again inside `focus_message_input`, again
        inside the typing gate, and the draft guard read once more - each a
        full parse of the whole thread (measured at 0.12s p50, 0.34s p95, on a
        thread of 25 bubbles). They are all asking about the same instant.

        So a read is reused while the extension has not published a newer
        state. That is not a cache with a guessed lifetime: the sequence is
        the page's own version number, and re-reading an unchanged sequence
        returns the identical payload by construction. Every mutation - a
        click, typing, an attachment - makes the extension publish, which
        moves the sequence on and forces the next caller to read for real.
        The safety property callers rely on ("the DOM as it is immediately
        before I act") is therefore unchanged.

        `MAX_REUSE_SECONDS` is the backstop for the one case the sequence
        cannot speak for: an extension that has stopped publishing at all. A
        frozen sequence then means "no news", not "nothing changed", so after
        that long the read is taken again regardless.

        `force=True` skips the reuse entirely, for a caller that wants to see
        the page move.
        """
        if self.dom_observer is None:
            return None
        if not force:
            reusable = await self._reusable_read()
            if reusable is not None:
                return reusable
        state = await self.dom_observer.observe_state()
        if state.messages_open and not state.thread_trusted:
            logger.error(
                "[VERIFY] rejected untrusted thread source=%s nodes=%s notices=%s",
                state.thread_source or "unknown",
                state.thread_nodes,
                state.thread_notices,
            )
            self._last_read = None
            return None
        self._last_read = (state, monotonic())
        return state

    async def _reusable_read(self):
        """The previous read, if the page has not published anything since."""
        if self._last_read is None:
            return None
        state, read_at = self._last_read
        sequence = int(getattr(state, "sequence", 0) or 0)
        if sequence <= 0:
            # A state with no sequence came from the raw-DOM fallback, which
            # carries no version to compare against.
            return None
        if monotonic() - read_at > MAX_REUSE_SECONDS:
            return None
        published = await self._published_sequence()
        if published is None or published != sequence:
            return None
        return state

    async def _published_sequence(self):
        """What the extension says its newest state is, without parsing it."""
        browser = getattr(self.dom_observer, "browser", None)
        reader = getattr(browser, "current_extension_sequence", None)
        if reader is None:
            return None
        try:
            return int(await reader() or 0)
        except Exception:
            # Cannot tell whether the page moved, so nothing is reused.
            logger.debug("[VERIFY] sequence read failed", exc_info=True)
            return None

    async def confirm_reply_after_inbound(
        self, inbound_message_id=None, text=None, previous_sequence=None,
        timeout_ms=None, inbound_order=None,
    ):
        """Confirm the thread now shows an OUTBOUND message after the inbound one.

        This is the only accepted proof that a reply landed. `text`, when
        given, additionally requires that the outbound message is exactly the
        approved reply, so an unrelated older answer can never be mistaken for
        this send. `previous_sequence` holds the check until the extension has
        published a newer state, so a pre-send snapshot is never read as
        success.

        `inbound_order` is the position of the inbound message the reply was
        actually generated for, captured before the send. When the DOM
        re-renders and `inbound_message_id` can no longer be found verbatim,
        the fallback must never drift forward onto whatever the customer has
        typed *since* - a chatty customer who keeps sending messages while
        this poll runs would otherwise shift the anchor past a reply that
        already landed, and a real send would be misread as unconfirmed.

        Returns `(confirmed, state, outbound)`; ambiguity is reported as
        `False` and never turned into an implicit retry.
        """
        if self.dom_observer is None:
            return True, None, None
        barrier = None if previous_sequence is None else int(previous_sequence or 0)

        def resolve_inbound(view):
            inbound = view.find(inbound_message_id)
            if inbound is not None:
                return inbound
            if inbound_order is None:
                return view.latest_inbound
            return next(
                (
                    item for item in reversed(view.messages)
                    if item.is_inbound and item.order <= inbound_order
                ),
                None,
            )

        def outbound_of(observed):
            view = observed.conversation_view
            inbound = resolve_inbound(view)
            if inbound is None:
                return None
            if text:
                return view.outbound_after_matching(inbound, text)
            return view.outbound_after(inbound)

        def replied(observed):
            if barrier is not None and observed.sequence and not observed.is_newer_than(
                barrier
            ):
                return False
            return outbound_of(observed) is not None

        state = await self.dom_observer.wait_for(
            replied,
            timeout_ms=self.timeout_ms if timeout_ms is None else timeout_ms,
            reason="reply_after_inbound",
        )
        outbound = outbound_of(state)
        confirmed = replied(state)
        logger.info(
            "[VERIFY] reply_after_inbound inbound=%s confirmed=%s outbound=%s "
            "sequence=%s previous=%s source=%s",
            inbound_message_id or "latest", confirmed,
            outbound.id if outbound else "none", state.sequence,
            "none" if barrier is None else barrier, state.source,
        )
        return confirmed, state, outbound

    async def confirm_composer_text(self, text, timeout_ms=None):
        """Confirm the composer holds exactly the approved text."""
        if self.dom_observer is None:
            return True, None
        wanted = " ".join(str(text).split())
        state = await self.dom_observer.wait_for(
            lambda observed: (
                " ".join(observed.composer_text.split()) == wanted
            ),
            timeout_ms=self.timeout_ms if timeout_ms is None else timeout_ms,
            reason="composer_text",
        )
        confirmed = " ".join(state.composer_text.split()) == wanted
        logger.info(
            "[VERIFY] composer matches_expected=%s present=%s",
            confirmed, state.composer_present,
        )
        return confirmed, state

    async def confirm_message_sent(self, text, timeout_ms=None):
        """Confirm the message is rendered in the thread and the composer cleared.

        Ambiguity is reported as `False`; it never becomes an implicit retry.
        """
        if self.dom_observer is None:
            return True, None
        state = await self.dom_observer.wait_for(
            lambda observed: (
                observed.contains_message(text)
                and not observed.composer_text.strip()
            ),
            timeout_ms=self.timeout_ms if timeout_ms is None else timeout_ms,
            reason="message_sent",
        )
        confirmed = state.contains_message(text) and not state.composer_text.strip()
        logger.info(
            "[VERIFY] send message_visible=%s composer_empty=%s confirmed=%s",
            state.contains_message(text), not state.composer_text.strip(), confirmed,
        )
        return confirmed, state

import json
import asyncio
import logging
from urllib.parse import urlparse

from core.logging_safety import content_fingerprint

from models.auth import AuthState, detect_auth_state
from models.conversation_view import direction_debug_block
from models.observation import Observation
from models.message_delivery import ComposerState, MessageSendState
from perception.dom_observer import DomObserver
from perception.state_parser import StateParser
from platforms.tiktok.screens import TikTokScreen
from platforms.tiktok_web.ui_states import screen_for_ui_state


logger = logging.getLogger(__name__)


class BrowserObserver:
    def __init__(
        self,
        browser,
        vision,
        confidence_threshold=0.80,
        target_confidence_threshold=0.85,
        max_vision_capture_attempts=3,
        vision_retry_delay_seconds=2,
        extension_event_wait_ms=1200,
        conversation_context_repository=None,
        dom_observer=None,
    ):
        self.browser = browser
        self.vision = vision
        self.confidence_threshold = float(confidence_threshold)
        self.target_confidence_threshold = float(target_confidence_threshold)
        self.max_vision_capture_attempts = max(1, int(max_vision_capture_attempts))
        self.vision_retry_delay_seconds = max(0.0, float(vision_retry_delay_seconds))
        self._last_confirmed_auth = AuthState.UNKNOWN
        self.extension_event_wait_ms = max(0, int(extension_event_wait_ms))
        self._last_observation_trace = None
        # The last thread that was logged, so an unchanged conversation is not
        # re-printed on every poll of the same screen.
        self._last_conversation_signature = None
        self.conversation_context_repository = conversation_context_repository
        self.dom_observer = dom_observer or DomObserver(browser, StateParser())

    def begin_task(self):
        if hasattr(self.browser, "begin_task"):
            self.browser.begin_task()

    async def observe(
        self, force_vision=False, after_action=None, expected_message=None,
        previous_extension_sequence=None, expected_screen=None,
        grounding_target=None, allow_vision=True,
    ):
        logger.info("[DOM] Observation started")
        logger.debug(
            "DOM observation context: after_action=%s force_vision=%s",
            after_action or "none", bool(force_vision),
        )
        event_received = False
        if after_action and hasattr(self.browser, "wait_for_perception_event"):
            event_received = await self.browser.wait_for_perception_event(
                self.extension_event_wait_ms,
                previous_sequence=previous_extension_sequence,
                expected_screen=expected_screen,
            )
            logger.debug(
                "Extension perception wait finished: after_action=%s event_received=%s",
                after_action, event_received,
            )
        try:
            snapshot = await self.dom_observer.snapshot()
        except Exception as exc:
            logger.warning(
                "DOM observation failed: error_type=%s", type(exc).__name__, exc_info=True,
            )
            logger.warning(
                "Screen recognition: status=unknown screen=%s confidence=0.00 "
                "source=dom auth=%s extension_sequence=0 vision_used=False",
                TikTokScreen.ERROR, AuthState.TEMPORARY_ERROR.value,
            )
            return Observation(
                screen=TikTokScreen.ERROR,
                confidence=0.0,
                screen_confidence=0.0,
                perception_source="dom",
                unknown=True,
                auth_state=AuthState.TEMPORARY_ERROR,
                metadata={"temporary_error": str(exc)},
            )

        extension_state = snapshot.get("extension_state") or {}
        extension_sequence = int(extension_state.get("sequence", 0) or 0)
        previous_sequence = (
            None if previous_extension_sequence is None
            else int(previous_extension_sequence or 0)
        )
        if after_action and previous_sequence is not None and extension_state:
            # Freshness is decided by the sequence barrier alone: any
            # `sequence > previous_sequence` is a new state. The extension may
            # publish several states for one action, so `previous + 1` is never
            # required, and a timed-out event wait does not turn a state that
            # already crossed the barrier into a stale one.
            if extension_sequence > previous_sequence:
                logger.info(
                    "[DOM] Extension state is fresh after action=%s sequence=%s "
                    "previous=%s event_received=%s",
                    after_action, extension_sequence, previous_sequence,
                    event_received,
                )
            else:
                # The barrier was not crossed. Ignore the stale Extension
                # classification and let the current browser DOM be the evidence.
                snapshot = dict(snapshot)
                snapshot["extension_state"] = None
                logger.info(
                    "[DOM] Ignoring stale Extension state after action=%s "
                    "sequence=%s previous=%s",
                    after_action, extension_sequence, previous_sequence,
                )

        dom_screen, dom_confidence = self._detect(snapshot)
        extension_state = snapshot.get("extension_state") or {}
        if extension_state:
            # A valid extension classification always outranks the browser DOM
            # fallback: the DOM heuristics (URL shape, flat element scan) cannot
            # see the inbox drawer, so they are only consulted when the
            # extension has nothing conclusive to say.
            extension_screen = screen_for_ui_state(extension_state.get("ui_state"))
            extension_confidence = float(
                extension_state.get("page_confidence", 0.0) or 0.0
            )
            if extension_screen != TikTokScreen.UNKNOWN and (
                dom_screen != extension_screen
            ):
                logger.info(
                    "[DOM] Extension state preferred over browser DOM fallback "
                    "extension=%s (%.2f) dom=%s (%.2f)",
                    extension_screen, extension_confidence,
                    dom_screen, dom_confidence,
                )
                dom_screen, dom_confidence = extension_screen, extension_confidence
            # Schema v2 is read directly; the ui_state -> TikTokScreen mapping
            # lives only in platforms.tiktok_web.ui_states.
            logger.info(
                "[DOM] Extension state received screen=%s confidence=%.2f sequence=%s",
                screen_for_ui_state(extension_state.get("ui_state")),
                float(extension_state.get("page_confidence", 0.0) or 0.0),
                extension_state.get("sequence", 0),
            )
            logger.info(
                "[EXT] state=%s mode=%s seq=%s elements=%s conversations=%s composer=%s",
                str(extension_state.get("ui_state", "UNKNOWN")).upper(),
                extension_state.get("messages_mode") or "none",
                extension_state.get("sequence", 0),
                len(extension_state.get("elements") or []),
                len(extension_state.get("conversations") or []),
                bool((extension_state.get("composer") or {}).get("present")),
            )
        else:
            logger.info(
                "[DOM] Browser DOM state used screen=%s confidence=%.2f",
                self._display_screen(dom_screen), dom_confidence,
            )
        auth_state = detect_auth_state(snapshot)
        if auth_state == AuthState.LOGGED_IN:
            self._last_confirmed_auth = AuthState.LOGGED_IN
        elif auth_state in {
            AuthState.AUTH_REQUIRED, AuthState.AUTH_RATE_LIMITED,
            AuthState.SECURITY_CHALLENGE,
        }:
            self._last_confirmed_auth = auth_state
        elif (
            auth_state == AuthState.TEMPORARY_ERROR
            and self._last_confirmed_auth == AuthState.LOGGED_IN
            and snapshot.get("session_cookie_names")
            and snapshot.get("page_loading")
            and not snapshot.get("login_form_visible")
        ):
            # A retained session cookie plus no explicit login/security wall
            # prevents transient TikTok render gaps from logging the agent out.
            # Explicit auth/security signals above always take precedence.
            auth_state = AuthState.LOGGED_IN
        security_challenge = auth_state in {
            AuthState.AUTH_RATE_LIMITED, AuthState.SECURITY_CHALLENGE,
        }
        temporary_error = auth_state == AuthState.TEMPORARY_ERROR
        if dom_screen == TikTokScreen.SECURITY_CHALLENGE:
            auth_state = AuthState.SECURITY_CHALLENGE
            security_challenge = True
            temporary_error = False
        if security_challenge:
            dom_screen = TikTokScreen.SECURITY_CHALLENGE
            dom_confidence = 1.0
        elif auth_state == AuthState.AUTH_REQUIRED:
            dom_screen = TikTokScreen.LOGIN
            dom_confidence = max(dom_confidence, 0.95)

        screen, confidence = dom_screen, dom_confidence
        # The open thread travels with the observation. Without this every
        # caller had to re-read the DOM to learn what the customer wrote, and a
        # thread whose messages the parser could not classify looked exactly
        # like a thread that had not rendered yet.
        conversation_messages = self._conversation_messages(
            extension_state, dom_screen
        )
        delivery = self._message_dom_state(
            snapshot, after_action=after_action, expected_message=expected_message
        )
        source = "extension" if snapshot.get("extension_state") else "dom"
        visual_targets = []
        visual = None
        screenshot = None
        extension_messages = [
            item for item in conversation_messages
            if str(item.get("text", "")).strip()
        ]
        dom_has_message_text = bool(extension_messages) or any(
            (
                str(element.get("resource_id", "")).casefold()
                == "dm-new-message-text"
                or "message-item" in str(
                    element.get("resource_id", "")
                ).casefold()
            )
            and str(element.get("text", "")).strip()
            for element in snapshot.get("elements", [])
            if element.get("visible", True)
        )
        extract_conversation = (
            dom_screen == TikTokScreen.CONVERSATION and not dom_has_message_text
        )
        needs_targets = self._critical_target_missing(
            dom_screen, snapshot["elements"], snapshot.get("extension_state")
        )
        should_use_vision = (
            (
                force_vision or dom_confidence < self.confidence_threshold
                or needs_targets
                or extract_conversation
                or self._verification_uncertain(after_action, delivery, dom_screen)
            )
            and not security_challenge
            and not temporary_error
            and allow_vision
            and getattr(self.vision, "enabled", True)
        )
        vision_reason = None
        if should_use_vision:
            if force_vision:
                vision_reason = "forced"
            elif dom_confidence < self.confidence_threshold:
                vision_reason = "low_confidence"
            elif needs_targets:
                vision_reason = "critical_target_missing"
            elif extract_conversation:
                vision_reason = "conversation_text_missing"
            else:
                vision_reason = "verification_uncertain"
            logger.info("[VISION] Fallback triggered reason=%s", vision_reason)
        logger.info(
            "DOM observation classified: screen=%s confidence=%.2f auth=%s vision_needed=%s",
            dom_screen, dom_confidence, auth_state.value, bool(should_use_vision),
        )
        if should_use_vision:
            for attempt in range(self.max_vision_capture_attempts):
                try:
                    logger.info(
                        "Vision capture started: attempt=%s/%s",
                        attempt + 1, self.max_vision_capture_attempts,
                    )
                    screenshot = await self.browser.screenshot()
                    logger.info("[VISION] Screenshot captured")
                    visual = await self.vision.analyze(
                        screenshot,
                        expected_message=expected_message,
                        verification_action=after_action,
                        grounding_target=grounding_target,
                        extract_conversation=extract_conversation,
                    )
                    visual_targets = [
                        target for target in visual.targets
                        if target.confidence >= self.target_confidence_threshold
                    ]
                    informative = self._vision_is_informative(
                        visual, after_action, visual_targets, grounding_target
                    )
                    logger.info(
                        "Vision result: attempt=%s screen=%s confidence=%.2f targets=%s informative=%s",
                        attempt + 1, visual.screen, visual.confidence,
                        len(visual_targets), bool(informative),
                    )
                    if visual.confidence > 0.0 or visual_targets:
                        source = (
                            "vision" if dom_screen == TikTokScreen.UNKNOWN
                            else "extension+vision" if snapshot.get("extension_state")
                            else "dom+vision"
                        )
                    if visual.confidence > dom_confidence:
                        screen, confidence = visual.screen, visual.confidence
                        if visual.source == "gemini":
                            source = visual.source
                    if visual.confidence >= self.confidence_threshold:
                        delivery = self._merge_delivery(delivery, visual)
                    if informative:
                        break
                except Exception as exc:
                    logger.warning(
                        "Vision capture stopped: attempt=%s error_type=%s",
                        attempt + 1, type(exc).__name__, exc_info=True,
                    )
                    screenshot = None
                    visual_targets = []
                    break
                if attempt + 1 < self.max_vision_capture_attempts:
                    await asyncio.sleep(self.vision_retry_delay_seconds)

        if (
            visual is not None
            and extract_conversation
            and visual.messages
            and self.conversation_context_repository is not None
        ):
            await self._persist_visual_conversation(snapshot, visual)

        if screen == TikTokScreen.SECURITY_CHALLENGE:
            auth_state = AuthState.SECURITY_CHALLENGE
            security_challenge = True
            visual_targets = []
        elif screen == TikTokScreen.LOGIN:
            auth_state = AuthState.AUTH_REQUIRED
            visual_targets = []

        if hasattr(self.browser, "set_visual_targets"):
            await self.browser.set_visual_targets(
                visual_targets, screenshot, self.target_confidence_threshold
            )
        recognition_status = self._recognition_status(
            screen, confidence, security_challenge
        )
        trace_key = (
            screen, round(confidence, 2), source, auth_state.value,
            recognition_status, delivery["message_send_state"].value,
            delivery["composer_state"].value,
        )
        if trace_key != self._last_observation_trace:
            logger.info(
                "[OBS] screen=%s confidence=%.2f source=%s status=%s auth=%s",
                self._display_screen(screen), confidence, source,
                recognition_status, auth_state.value,
            )
            self._last_observation_trace = trace_key
        return Observation(
            screen=screen,
            confidence=confidence,
            screen_confidence=confidence,
            perception_source=source,
            visual_targets=visual_targets,
            message_send_state=delivery["message_send_state"],
            composer_state=delivery["composer_state"],
            composer_text_matches_expected=delivery["composer_text_matches_expected"],
            last_message_visible=delivery["last_message_visible"],
            elements=snapshot["elements"],
            screenshot_path=screenshot,
            raw_ui_xml=json.dumps({
                "url": snapshot["url"], "title": snapshot["title"],
                "signature": snapshot["signature"],
            }, ensure_ascii=False),
            unknown=confidence < self.confidence_threshold,
            security_challenge=security_challenge,
            auth_state=auth_state,
            metadata={
                "url": snapshot["url"], "title": snapshot["title"],
                "signature": snapshot["signature"], "body_text": snapshot["body_text"][:10000],
                "session_cookie_names": snapshot.get("session_cookie_names", []),
                "authenticated_dom": snapshot.get("authenticated_dom", False),
                "extension_state": snapshot.get("extension_state"),
                "conversation_messages": conversation_messages,
                "viewport": snapshot.get("viewport"),
                "blocking_overlay_present": snapshot.get(
                    "blocking_overlay_present", False
                ),
                "ui_abnormal": snapshot.get("ui_abnormal", True),
            },
        )

    def _conversation_messages(self, extension_state, screen):
        """The rendered thread, logged once per observation of a conversation.

        Returns the parsed messages as the extension reported them. A message
        whose direction the parser could not resolve is logged with the DOM
        metadata it carried, because silently dropping it is what left
        customers unanswered.
        """
        messages = [
            item for item in (extension_state or {}).get("messages") or ()
            if isinstance(item, dict)
        ]
        if screen != TikTokScreen.CONVERSATION:
            return messages
        if not extension_state:
            # No extension state to judge: it was stale after an action and
            # dropped (see `observe`), so the screen came from the DOM alone.
            # Nothing was read, so there is nothing untrusted to report.
            return []
        thread = extension_state.get("thread") or {}
        thread_source = str(thread.get("container") or "").strip()
        if thread_source not in {"message_list", "chat_box", "structure"}:
            logger.error(
                "[INBOX] rejected untrusted raw thread source=%s candidates=%s",
                thread_source or "unknown", thread.get("nodes", 0),
            )
            return []
        if not messages:
            # An empty thread is only alarming when the parser did look: it
            # separates "not rendered yet" from "read the wrong container".
            logger.info(
                "[INBOX] thread carried no message container=%s candidates=%s notices=%s",
                thread.get("container", "none"), thread.get("nodes", 0),
                thread.get("notices", 0),
            )
            return messages
        signature = tuple(
            (str(item.get("id", "")), str(item.get("direction", ""))) for item in messages
        )
        if signature == self._last_conversation_signature:
            return messages
        self._last_conversation_signature = signature
        logger.info(
            "[INBOX] thread read container=%s candidates=%s messages=%s notices=%s frame=%s",
            thread.get("container", "none"), thread.get("nodes", len(messages)),
            len(messages), thread.get("notices", 0), thread.get("frame_width"),
        )
        for item in messages:
            logger.info(
                "[INBOX] message parsed text_hash=%s characters=%s direction=%s "
                "sender=%s id=%s source=%s",
                content_fingerprint(item.get("text", "")),
                len(str(item.get("text", ""))),
                item.get("direction", "UNKNOWN"),
                item.get("sender") or "unknown",
                item.get("id", ""),
                item.get("direction_source") or "none",
            )
            if str(item.get("direction", "UNKNOWN")).upper() == "UNKNOWN":
                self._log_unresolved_direction(item)
        return messages

    @staticmethod
    def _log_unresolved_direction(message):
        """Everything the parser saw about a bubble it could not place."""
        logger.warning(
            "[INBOX][DEBUG] direction unresolved\n%s",
            direction_debug_block(message),
        )

    async def _persist_visual_conversation(self, snapshot, visual):
        extension_state = snapshot.get("extension_state") or {}
        parsed = urlparse(snapshot.get("url", ""))
        path_parts = [part for part in parsed.path.split("/") if part]
        conversation_id = (
            path_parts[1]
            if len(path_parts) >= 2 and path_parts[0] == "messages"
            else str((extension_state.get("active_chat") or {}).get("username") or "").strip()
        )
        if not conversation_id:
            logger.warning("[VISION] conversation extraction skipped: missing conversation id")
            return
        active_chat = extension_state.get("active_chat") or {}
        active_username = str(active_chat.get("username") or "").strip()
        active_avatar_url = str(active_chat.get("avatar_url") or "").strip()
        await self.conversation_context_repository.store_visible_messages(
            conversation_id,
            visual.messages,
            customer_id=conversation_id,
            source="vision",
            platform="tiktok_web",
            username=active_username,
            display_name=active_username,
            avatar_url=active_avatar_url or None,
        )
        latest_inbound = next((
            message for message in reversed(visual.messages)
            if message.direction == "INBOUND"
        ), None)
        if visual.customer_facts and latest_inbound is not None:
            digest = self.conversation_context_repository.message_hash(
                conversation_id, latest_inbound.direction, latest_inbound.text
            )
            await self.conversation_context_repository.merge_facts(
                conversation_id,
                visual.customer_facts,
                source_message_id=f"vision-{digest[:32]}",
                confidence=visual.confidence,
                username=active_username or None,
                display_name=active_username or None,
            )

    def _recognition_status(self, screen, confidence, security_challenge):
        if security_challenge or screen == TikTokScreen.SECURITY_CHALLENGE:
            return "human_required"
        if screen in {None, TikTokScreen.UNKNOWN, TikTokScreen.ERROR}:
            return "unknown"
        if confidence < self.confidence_threshold:
            return "low_confidence"
        return "recognized"

    @staticmethod
    def _display_screen(screen):
        return str(screen or "UNKNOWN").removeprefix("TIKTOK_")

    @staticmethod
    def _message_dom_state(snapshot, after_action=None, expected_message=None):
        expected = (expected_message or "").strip()
        visible = [
            element for element in snapshot.get("elements", [])
            if element.get("visible", True)
        ]
        composer_ids = {"dm-new-input-editor", "message-input-area"}
        extension_composer = (snapshot.get("extension_state") or {}).get("composer") or {}
        if extension_composer.get("present"):
            # The extension resolved the editable node itself, so its text is
            # the typed value. The surrounding elements carry TikTok's
            # placeholder ("Send a message..."), which would otherwise read as
            # a permanently non-empty composer.
            composers = [str(extension_composer.get("text", "")).strip()]
        else:
            composers = [
                str(element.get("text", "")).strip()
                for element in visible
                if str(element.get("resource_id", "")).casefold() in composer_ids
                or element.get("role") in {"textarea", "textbox", "message_input"}
            ]
        if not composers:
            composer_state = ComposerState.UNKNOWN
            composer_matches = None
        else:
            composer_text = next((text for text in composers if text), "")
            composer_state = (
                ComposerState.HAS_TEXT if composer_text else ComposerState.EMPTY
            )
            composer_matches = composer_text == expected if expected else None

        last_message_visible = None
        if expected:
            last_message_visible = any(
                str(item.get("text", "")).strip() == expected
                for item in (snapshot.get("extension_state") or {}).get("messages", [])
            ) or any(
                str(element.get("resource_id", "")) == "dm-new-message-text"
                and str(element.get("text", "")).strip() == expected
                for element in visible
            )

        send_state = MessageSendState.UNCERTAIN
        evidence = snapshot.get("last_send_evidence") or {}
        if after_action in {"platform.send_message", "platform.retry_send"}:
            same_message = evidence.get("expected_text") == expected
            if same_message and evidence.get("message_count_increased") and (
                evidence.get("composer_text", "") == ""
            ):
                send_state = MessageSendState.SENT
                last_message_visible = True
            elif last_message_visible is True and composer_state == ComposerState.EMPTY:
                send_state = MessageSendState.SENT
            elif composer_matches is True and last_message_visible is False:
                send_state = MessageSendState.NOT_SENT
            else:
                send_state = MessageSendState.UNCERTAIN

        return {
            "message_send_state": send_state,
            "composer_state": composer_state,
            "composer_text_matches_expected": composer_matches,
            "last_message_visible": last_message_visible,
        }

    @staticmethod
    def _verification_uncertain(after_action, delivery, screen):
        if after_action == "platform.open_conversation":
            return screen != TikTokScreen.CONVERSATION
        if after_action == "browser.input_text":
            return delivery["composer_text_matches_expected"] is not True
        if after_action in {"platform.send_message", "platform.retry_send"}:
            return delivery["message_send_state"] == MessageSendState.UNCERTAIN
        return False

    @staticmethod
    def _merge_delivery(dom, visual):
        merged = dict(dom)
        if dom["message_send_state"] == MessageSendState.UNCERTAIN:
            merged["message_send_state"] = visual.message_send_state
        if dom["composer_state"] == ComposerState.UNKNOWN:
            merged["composer_state"] = visual.composer_state
        if dom["composer_text_matches_expected"] is None:
            merged["composer_text_matches_expected"] = visual.composer_text_matches_expected
        if dom["last_message_visible"] is None:
            merged["last_message_visible"] = visual.last_message_visible
        # Empty composer without a visible sent message is never evidence for
        # retry or success; preserve the conservative UNCERTAIN state.
        if (
            merged["composer_state"] == ComposerState.EMPTY
            and merged["last_message_visible"] is not True
        ):
            merged["message_send_state"] = MessageSendState.UNCERTAIN
        return merged

    def _vision_is_informative(
        self, visual, after_action, visual_targets, grounding_target=None
    ):
        if grounding_target:
            return any(
                target.type == grounding_target
                and target.confidence >= self.target_confidence_threshold
                for target in visual_targets
            )
        if visual.confidence < self.confidence_threshold:
            return False
        if visual.screen == TikTokScreen.UNKNOWN:
            return False
        if after_action == "platform.open_conversation":
            return visual.screen == TikTokScreen.CONVERSATION
        if after_action == "browser.input_text":
            return (
                visual.composer_state != ComposerState.UNKNOWN
                and visual.composer_text_matches_expected is not None
            )
        if after_action in {"platform.send_message", "platform.retry_send"}:
            return (
                visual.message_send_state != MessageSendState.UNCERTAIN
                or visual.composer_state != ComposerState.UNKNOWN
                or visual.last_message_visible is not None
            )
        return bool(visual_targets) or visual.screen != TikTokScreen.UNKNOWN

    @staticmethod
    def _critical_target_missing(screen, elements, extension_state=None):
        """True when the screen lacks the element the next step needs.

        Semantic roles from the extension answer this directly; the flat
        `resource_id` check is only the fallback for a plain DOM snapshot.
        """
        if extension_state:
            if screen == TikTokScreen.MESSAGES:
                return not (extension_state.get("conversations") or [])
            if screen == TikTokScreen.CONVERSATION:
                return not (extension_state.get("composer") or {}).get("present")
            return False
        visible = [element for element in elements if element.get("visible", True)]
        resource_ids = {
            str(element.get("resource_id", "")).casefold() for element in visible
        }
        roles = {str(element.get("role", "")).casefold() for element in visible}
        if screen == TikTokScreen.MESSAGES:
            return "conversation" not in roles and "dm-new-conversation-item" not in resource_ids
        if screen == TikTokScreen.CONVERSATION:
            return "message_input" not in roles and not resource_ids.intersection({
                "dm-new-input-editor", "message-input-area",
            })
        return False

    @staticmethod
    def _detect_auth(snapshot):
        """Compatibility entry point; classification lives in models.auth."""
        return detect_auth_state(snapshot)

    @staticmethod
    def _detect(snapshot):
        return StateParser.detect(snapshot)

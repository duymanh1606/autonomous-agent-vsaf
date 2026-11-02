import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from models.conversation_view import ConversationView, normalize_direction
from platforms.tiktok.screens import TikTokScreen
from platforms.tiktok_web.selectors import (
    CHAT_HEADER_RESOURCE_IDS,
    UNREAD_LABEL_WORDS,
)
from platforms.tiktok_web.ui_states import UI_STATE_TO_SCREEN


COMPOSER_RESOURCE_IDS = frozenset({"dm-new-input-editor", "message-input-area"})
CONVERSATION_ROW_RESOURCE_ID = "dm-new-conversation-item"
MESSAGE_TEXT_RESOURCE_ID = "dm-new-message-text"
SEND_BUTTON_RESOURCE_ID = "message-send"
# `inbox-icon` is TikTok's Activity/notification entry, not Messages, so it is
# deliberately absent here.
MESSAGES_NAV_RESOURCE_IDS = frozenset({"nav-message", "nav-messages", "top-dm-icon"})

_BADGE_NUMBER = re.compile(r"\d+")


@dataclass(frozen=True)
class ConversationRow:
    """One conversation row exactly as it is currently rendered."""

    name: str
    unread: bool = False
    preview: str = ""
    index: int = 0
    selector: str = ""
    message_request: bool = False
    conversation_id: str = ""


@dataclass(frozen=True)
class ParsedMessage:
    """A visible message. `direction` is never inferred from screen position.

    `index` is the position inside the rendered thread and `timestamp` the
    absolute time when the platform exposed one; together they answer "which
    message came after which" without guessing.
    """

    text: str
    direction: str = "UNKNOWN"
    message_id: str | None = None
    index: int = 0
    timestamp: float | None = None
    media: tuple[dict, ...] = field(default_factory=tuple)

    @property
    def id(self) -> str:
        return self.message_id or f"message-{self.index}"


@dataclass(frozen=True)
class ParsedDomState:
    """Normalized, read-only view of the TikTok Web DOM at one point in time."""

    screen: str
    confidence: float
    source: str = "dom"
    # The extension's monotonic state counter. `0` means "no extension state",
    # so callers must treat it as "sequence unknown" rather than "state 0".
    sequence: int = 0
    url: str = ""
    viewport: tuple[int | None, int | None] = (None, None)
    unread_badge: bool = False
    unread_count: int = 0
    activity_unread_count: int = 0
    messages_mode: str | None = None
    messages_panel_present: bool = False
    messages_panel_visible: bool = False
    active_chat: str = ""
    active_chat_present: bool = False
    conversations: tuple[ConversationRow, ...] = field(default_factory=tuple)
    messages: tuple[ParsedMessage, ...] = field(default_factory=tuple)
    thread_source: str = ""
    thread_trusted: bool = False
    thread_nodes: int = 0
    thread_notices: int = 0
    composer_present: bool = False
    composer_text: str = ""
    send_button_present: bool = False
    send_button_enabled: bool = False
    blocking_overlay_present: bool = False
    ui_abnormal: bool = True

    @property
    def unread_conversations(self) -> tuple[str, ...]:
        return tuple(row.name for row in self.conversations if row.unread and row.name)

    @property
    def has_unread(self) -> bool:
        """True when any explicit unread signal is visible on this screen.

        Only the Messages badge and unread conversation rows count. The
        Activity/notification badge is deliberately excluded.
        """
        return bool(self.unread_badge or self.unread_conversations)

    @property
    def messages_open(self) -> bool:
        """True for both the inbox drawer and the full Messages page."""
        return self.screen == TikTokScreen.MESSAGES or self.messages_panel_visible

    @property
    def conversation_open(self) -> bool:
        """DOM-strong evidence only: an active chat plus a composer."""
        return bool(self.active_chat_present and self.composer_present)

    @property
    def last_inbound_message(self) -> ParsedMessage | None:
        return next(
            (item for item in reversed(self.messages) if item.direction == "INBOUND"),
            None,
        )

    @property
    def conversation_view(self) -> ConversationView:
        """The thread as an ordered view; the source of truth for "replied?"."""
        return ConversationView.from_items(
            {
                "id": item.id,
                "text": item.text,
                "direction": item.direction,
                "timestamp": item.timestamp,
                "index": item.index,
                "media": list(item.media),
            }
            for item in self.messages
        )

    def already_replied(self, inbound_message_id=None) -> bool:
        """True when an OUTBOUND message follows the relevant inbound one."""
        view = self.conversation_view
        return view.already_replied(
            view.find(inbound_message_id) or view.latest_inbound
        )

    def contains_message(self, text: str) -> bool:
        wanted = " ".join(str(text).split()).casefold()
        if not wanted:
            return False
        return any(
            " ".join(item.text.split()).casefold() == wanted for item in self.messages
        )

    def is_screen(self, expected) -> bool:
        return bool(expected) and self.screen == expected

    def is_any_screen(self, expected) -> bool:
        """True when the screen is one of `expected` (a single value or a set)."""
        if not expected:
            return False
        if isinstance(expected, (str, bytes)):
            return self.screen == expected
        return self.screen in set(expected)

    def is_newer_than(self, previous_sequence) -> bool:
        """True when this state was emitted after `previous_sequence`.

        Only `sequence > previous_sequence` is required; the extension may
        publish several states for one action, so `previous + 1` is never
        assumed. A missing sequence (plain DOM snapshot) cannot prove
        freshness and answers `False`.
        """
        if previous_sequence is None or not self.sequence:
            return False
        return self.sequence > int(previous_sequence or 0)


class StateParser:
    """Convert a Playwright/extension DOM snapshot into TikTok UI state."""

    @classmethod
    def parse(cls, snapshot) -> ParsedDomState:
        screen, confidence = cls.detect(snapshot)
        extension = snapshot.get("extension_state") or {}
        viewport = snapshot.get("viewport") or {}
        thread = extension.get("thread") or {}
        thread_source = str(thread.get("container") or "").strip()
        thread_trusted = thread_source in {
            "message_list", "chat_box", "structure",
        }
        conversations = (
            cls._extension_conversations(extension)
            if extension.get("conversations") is not None
            else cls._dom_conversations(snapshot)
        )
        if extension.get("messages") is not None:
            messages = (
                cls._extension_messages(extension) if thread_trusted else ()
            )
        else:
            messages = cls._dom_messages(snapshot)
        composer = cls._composer(snapshot, extension)
        send_button = cls._send_button(snapshot, extension)
        unread_badge, unread_count = cls._unread_badge(snapshot, extension, conversations)
        panel = extension.get("messages_panel") or {}
        active_chat = extension.get("active_chat") or {}
        return ParsedDomState(
            screen=screen,
            confidence=confidence,
            source="extension" if extension else "dom",
            sequence=int(extension.get("sequence", 0) or 0),
            url=str(snapshot.get("url") or ""),
            viewport=(viewport.get("width"), viewport.get("height")),
            unread_badge=unread_badge,
            unread_count=unread_count,
            activity_unread_count=int(extension.get("activity_unread_count", 0) or 0),
            messages_mode=extension.get("messages_mode") or None,
            messages_panel_present=bool(panel.get("present")),
            messages_panel_visible=bool(panel.get("visible")),
            active_chat=str(active_chat.get("username") or "").strip(),
            active_chat_present=bool(
                active_chat.get("present")
                if extension else cls._dom_active_chat_present(snapshot)
            ),
            conversations=conversations,
            messages=messages,
            thread_source=thread_source,
            thread_trusted=thread_trusted,
            thread_nodes=int(thread.get("nodes", 0) or 0),
            thread_notices=int(thread.get("notices", 0) or 0),
            composer_present=composer["present"],
            composer_text=composer["text"],
            send_button_present=send_button["present"],
            send_button_enabled=send_button["enabled"],
            blocking_overlay_present=bool(
                snapshot.get("blocking_overlay_present", False)
            ),
            ui_abnormal=bool(snapshot.get("ui_abnormal", True)),
        )

    @staticmethod
    def _visible_elements(snapshot):
        return [
            element for element in snapshot.get("elements", ())
            if element.get("visible", True)
        ]

    @staticmethod
    def _extension_conversations(extension):
        """Rows the extension already scoped to the Messages panel/page."""
        rows = []
        for index, item in enumerate(extension.get("conversations") or ()):
            name = str(item.get("username") or item.get("text") or "").strip()
            if not name:
                continue
            rows.append(ConversationRow(
                name=name,
                unread=bool(item.get("unread")),
                preview=str(item.get("preview") or "").strip(),
                index=index,
                selector=str(item.get("selector") or ""),
                message_request=bool(item.get("message_request")),
                # `key` is the platform's own thread id; `id` is only the
                # row's position in this snapshot and never identifies a
                # conversation across scans.
                conversation_id=str(item.get("key") or ""),
            ))
        return tuple(rows)

    @classmethod
    def _dom_active_chat_present(cls, snapshot):
        return any(
            str(element.get("resource_id", "")).casefold() in CHAT_HEADER_RESOURCE_IDS
            and str(element.get("text", "")).strip()
            for element in cls._visible_elements(snapshot)
        )

    @classmethod
    def _dom_conversations(cls, snapshot):
        rows = []
        for index, element in enumerate(cls._visible_elements(snapshot)):
            # Only rows the platform explicitly marks as conversations. A
            # generic `[aria-label]`/nav scan would pull in For You, Explore,
            # Following, Friends, LIVE, Upload and Profile.
            if (
                str(element.get("role", "")).casefold() != "conversation"
                and str(element.get("resource_id", "")).casefold()
                != CONVERSATION_ROW_RESOURCE_ID
            ):
                continue
            raw = str(element.get("text") or "").strip()
            name = raw.splitlines()[0].strip() if raw else ""
            if not name:
                continue
            label = str(element.get("description") or "").casefold()
            rows.append(ConversationRow(
                name=name,
                # A plain snapshot cannot see per-row unread attributes; only an
                # explicit ARIA label counts. Row order is never treated as
                # unread evidence.
                unread=any(word in label for word in UNREAD_LABEL_WORDS),
                preview=" ".join(raw.splitlines()[1:]).strip(),
                index=index,
            ))
        return tuple(rows)

    @staticmethod
    def _extension_messages(extension):
        messages = []
        for position, item in enumerate(extension.get("messages") or ()):
            text = str(item.get("text") or "").strip()
            raw_media = item.get("media") or ()
            if isinstance(raw_media, dict):
                raw_media = (raw_media,)
            media = tuple(value for value in raw_media if isinstance(value, dict))
            if not text and not media:
                continue
            direction = normalize_direction(item.get("direction"))
            index = item.get("index")
            messages.append(ParsedMessage(
                text=text,
                direction=direction,
                message_id=str(item.get("id") or "") or None,
                index=(
                    int(index)
                    if isinstance(index, int) and not isinstance(index, bool)
                    else position
                ),
                timestamp=item.get("timestamp"),
                media=media,
            ))
        return tuple(messages)

    @classmethod
    def _dom_messages(cls, snapshot):
        messages = []
        for element in cls._visible_elements(snapshot):
            resource_id = str(element.get("resource_id", "")).casefold()
            if resource_id != MESSAGE_TEXT_RESOURCE_ID and "message-item" not in resource_id:
                continue
            text = str(element.get("text") or "").strip()
            if text:
                # Direction is not available in the flat snapshot and must not
                # be guessed from left/right placement. Order still is: the
                # snapshot lists elements in document order.
                messages.append(ParsedMessage(
                    text=text, direction="UNKNOWN", index=len(messages)
                ))
        return tuple(messages)

    @classmethod
    def _composer(cls, snapshot, extension):
        extension_composer = extension.get("composer")
        if isinstance(extension_composer, dict):
            return {
                "present": bool(extension_composer.get("present")),
                "text": str(extension_composer.get("text") or "").strip(),
            }
        texts = [
            str(element.get("text") or "").strip()
            for element in cls._visible_elements(snapshot)
            if str(element.get("resource_id", "")).casefold() in COMPOSER_RESOURCE_IDS
            or element.get("role") in {"textarea", "textbox", "message_input"}
        ]
        if not texts:
            return {"present": False, "text": ""}
        return {"present": True, "text": next((item for item in texts if item), "")}

    @classmethod
    def _send_button(cls, snapshot, extension):
        extension_send = extension.get("send_button")
        if isinstance(extension_send, dict):
            return {
                "present": bool(extension_send.get("present")),
                "enabled": bool(extension_send.get("enabled")),
            }
        for element in cls._visible_elements(snapshot):
            resource_id = str(element.get("resource_id", "")).casefold()
            label = str(element.get("description") or "").casefold()
            if resource_id == SEND_BUTTON_RESOURCE_ID or label in {"send", "gửi"}:
                return {"present": True, "enabled": bool(element.get("enabled", True))}
        return {"present": False, "enabled": False}

    @classmethod
    def _unread_badge(cls, snapshot, extension, conversations):
        """Messages unread only.

        The Activity/notification badge lives in a different nav entry and is
        never mixed in: `Messages badge = 1` and `Activity badge = 23` must not
        become `unread_count = 2` (or 24).
        """
        unread_rows = [row for row in conversations if row.unread]
        if extension:
            count = int(extension.get("messages_unread_count", 0) or 0)
            if not count:
                # Older payload shape: fall back to the semantic nav element.
                count = cls._messages_nav_badge(snapshot)
            if count > 0:
                return True, count
            if unread_rows:
                return True, len(unread_rows)
            return False, 0

        count = cls._messages_nav_badge(snapshot)
        if count > 0:
            return True, count
        if unread_rows:
            return True, len(unread_rows)
        return False, 0

    @classmethod
    def _messages_nav_badge(cls, snapshot):
        """Read the badge from the `messages_nav` element, never from Activity."""
        for element in cls._visible_elements(snapshot):
            role = str(element.get("role", "")).casefold()
            resource_id = str(element.get("resource_id", "")).casefold()
            label = str(element.get("description") or "").casefold()
            is_messages_entry = (
                role == "messages_nav"
                or resource_id in MESSAGES_NAV_RESOURCE_IDS
                or (
                    # A textual match is only trusted when it is not the
                    # Activity/notification entry.
                    ("message" in label or "tin nhắn" in label)
                    and "notification" not in label
                    and "thông báo" not in label
                    and "activity" not in label
                )
            )
            if not is_messages_entry:
                continue
            if element.get("unread") is True:
                badge = _BADGE_NUMBER.search(str(element.get("text") or ""))
                return int(badge.group()) if badge else 1
            if any(word in label for word in UNREAD_LABEL_WORDS):
                match = _BADGE_NUMBER.search(label)
                return int(match.group()) if match else 1
            badge = _BADGE_NUMBER.search(str(element.get("text") or ""))
            if badge and int(badge.group()) > 0:
                return int(badge.group())
        return 0

    @staticmethod
    def detect(snapshot):
        """Classify the screen.

        Extension schema v2 is authoritative and is translated through the one
        mapping in `platforms.tiktok_web.ui_states`. The URL heuristics below
        only run when no extension state is available at all.
        """
        extension = snapshot.get("extension_state") or {}
        ui_state = str(extension.get("ui_state") or "").strip().upper()
        if ui_state in UI_STATE_TO_SCREEN:
            confidence = float(extension.get("page_confidence", 0.0) or 0.0)
            if ui_state != "UNKNOWN" and confidence >= 0.5:
                return UI_STATE_TO_SCREEN[ui_state], confidence

        parsed = urlparse(snapshot.get("url", ""))
        path = parsed.path.rstrip("/") or "/"
        text = str(snapshot.get("body_text") or "").casefold()
        if not parsed.hostname or not (
            parsed.hostname == "tiktok.com" or parsed.hostname.endswith(".tiktok.com")
        ):
            return TikTokScreen.UNKNOWN, 0.0
        resource_ids = {
            str(element.get("resource_id", "")).casefold()
            for element in snapshot.get("elements", ())
        }
        if "dm-new-input-editor" in resource_ids or any(
            "message-input" in value for value in resource_ids
        ):
            return TikTokScreen.CONVERSATION, 0.99
        if any("comment-list" in value or "comment-title" in value for value in resource_ids):
            return TikTokScreen.COMMENTS, 0.95
        if any("notification" in value for value in resource_ids):
            return TikTokScreen.NOTIFICATIONS, 0.92
        if path.startswith("/login"):
            return TikTokScreen.LOGIN, 0.95
        if path.startswith("/messages/"):
            return TikTokScreen.CONVERSATION, 0.98
        if path == "/messages":
            return TikTokScreen.MESSAGES, 0.98
        if path.startswith("/search"):
            return TikTokScreen.SEARCH_RESULTS, 0.98
        if "/video/" in path:
            return TikTokScreen.FEED, 0.92
        if path.startswith("/@"):
            return TikTokScreen.PROFILE, 0.98
        if "upload" in path:
            return TikTokScreen.UPLOAD, 0.95
        if path in {"/", "/foryou", "/following", "/explore"}:
            return TikTokScreen.FEED, 0.95
        if "log in" in text or "đăng nhập" in text:
            return TikTokScreen.LOGIN, 0.88
        return TikTokScreen.UNKNOWN, 0.0

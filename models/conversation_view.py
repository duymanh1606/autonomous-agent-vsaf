"""DOM truth about one open conversation.

Every "did we already answer this customer?" decision is made here, from the
messages that are actually rendered in the open thread. Checkpoints, the task
database and send-task signatures are only ever *hints*: this view is the final
authority, so a lost or stale piece of bookkeeping can never make the agent skip
a real, unanswered message - and can never make it answer twice either.
"""

from dataclasses import dataclass

from core.logging_safety import content_fingerprint


INBOUND = "INBOUND"
OUTBOUND = "OUTBOUND"
UNKNOWN = "UNKNOWN"

_DIRECTIONS = {INBOUND, OUTBOUND}
_INBOUND_ALIASES = {"IN", "INCOMING", "RECEIVED", "CUSTOMER"}
_OUTBOUND_ALIASES = {"OUT", "OUTGOING", "SENT", "SELF", "AGENT", "ME"}


def normalize_text(value) -> str:
    return " ".join(str(value or "").split())


def normalize_direction(value) -> str:
    direction = str(value or "").strip().upper()
    if direction in _DIRECTIONS:
        return direction
    if direction in _INBOUND_ALIASES:
        return INBOUND
    if direction in _OUTBOUND_ALIASES:
        return OUTBOUND
    return UNKNOWN


def _timestamp(value) -> float | None:
    """Epoch seconds, or `None` when the platform exposed no absolute time."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        # Extensions report milliseconds; anything past year ~5138 is ms.
        return seconds / 1000.0 if seconds > 1e11 else seconds
    text = str(value).strip()
    if not text:
        return None
    try:
        return _timestamp(float(text))
    except ValueError:
        pass
    from datetime import datetime

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass(frozen=True)
class ViewMessage:
    """One rendered message. Order is explicit; direction is never guessed."""

    id: str
    text: str
    direction: str = UNKNOWN
    # Who wrote it, as the platform rendered it. Display only: `direction` is
    # what every decision is made on.
    sender: str = ""
    timestamp: float | None = None
    order: int = 0
    media: tuple[dict, ...] = ()

    @property
    def is_inbound(self) -> bool:
        return self.direction == INBOUND

    @property
    def is_outbound(self) -> bool:
        return self.direction == OUTBOUND

    @property
    def has_media(self) -> bool:
        return bool(self.media)

    def matches_text(self, text) -> bool:
        wanted = normalize_text(text).casefold()
        return bool(wanted) and normalize_text(self.text).casefold() == wanted

    def is_after(self, other: "ViewMessage") -> bool:
        """True when this message was rendered after `other`.

        Timestamps decide when both messages carry one and they differ;
        otherwise DOM order decides, which the extractor always provides.
        """
        if (
            self.timestamp is not None
            and other.timestamp is not None
            and self.timestamp != other.timestamp
        ):
            return self.timestamp > other.timestamp
        return self.order > other.order


class ConversationView:
    """Read-only, ordered view of the messages currently in the thread."""

    def __init__(self, messages=()):
        self.messages = tuple(sorted(messages, key=lambda item: item.order))

    def __len__(self):
        return len(self.messages)

    def __bool__(self):
        return bool(self.messages)

    # ------------------------------------------------------------- builders
    @classmethod
    def from_items(cls, items) -> "ConversationView":
        """Build from the raw `messages[]` the extension/DOM parser produced."""
        messages = []
        for position, item in enumerate(items or ()):
            get = item.get if isinstance(item, dict) else (
                lambda key, default=None, source=item: getattr(source, key, default)
            )
            text = normalize_text(get("text"))
            raw_media = get("media") or ()
            if isinstance(raw_media, dict):
                raw_media = (raw_media,)
            media = tuple(item for item in raw_media if isinstance(item, dict))
            if not text and not media:
                continue
            index = get("index")
            if index is None:
                index = get("order")
            order = int(index) if isinstance(index, int) and not isinstance(index, bool) else position
            direction = normalize_direction(get("direction"))
            identifier = str(
                get("id") or get("message_id") or ""
            ).strip() or f"message-{order}"
            messages.append(ViewMessage(
                id=identifier,
                text=text,
                direction=direction,
                sender=str(get("sender") or "").strip(),
                timestamp=_timestamp(get("timestamp")),
                order=order,
                media=media,
            ))
        return cls(messages)

    @classmethod
    def from_observation(cls, observation) -> "ConversationView":
        """The thread the observation carries.

        `conversation_messages` is what the observer parsed for this screen;
        the raw extension payload stays the fallback so an observation built
        before that field existed still reads.
        """
        metadata = getattr(observation, "metadata", None) or {}
        carried = metadata.get("conversation_messages")
        if carried:
            return cls.from_items(carried)
        extension_state = metadata.get("extension_state") or {}
        return cls.from_items(extension_state.get("messages") or ())

    @classmethod
    def from_dom_state(cls, state) -> "ConversationView":
        return cls.from_items(getattr(state, "messages", ()) or ())

    # -------------------------------------------------------------- queries
    @property
    def latest_inbound(self) -> ViewMessage | None:
        """The newest customer message. Screen position is never evidence."""
        return next(
            (item for item in reversed(self.messages) if item.is_inbound), None
        )

    @property
    def latest_outbound(self) -> ViewMessage | None:
        return next(
            (item for item in reversed(self.messages) if item.is_outbound), None
        )

    def pending_inbound(self) -> tuple[ViewMessage, ...]:
        """Every customer message that arrived after our last answer.

        This is the burst the next reply has to cover: a customer who writes
        "Shop ơi" / "Tôi muốn tư vấn" / "Còn hàng không?" is asking one thing,
        not three. Empty when the last message in the thread is ours.
        """
        last_outbound = self.latest_outbound
        return tuple(
            item for item in self.messages
            if item.is_inbound
            and (last_outbound is None or item.is_after(last_outbound))
        )

    def outbound_after(self, inbound: ViewMessage | None) -> ViewMessage | None:
        """The first message we sent after `inbound`, if there is one."""
        if inbound is None:
            return None
        return next(
            (
                item for item in self.messages
                if item.is_outbound and item.is_after(inbound)
            ),
            None,
        )

    def outbound_after_matching(self, inbound, text) -> ViewMessage | None:
        """An outbound copy of exactly `text` rendered after `inbound`."""
        if inbound is None:
            return None
        return next(
            (
                item for item in self.messages
                if item.is_outbound
                and item.is_after(inbound)
                and item.matches_text(text)
            ),
            None,
        )

    def outbound_media_after(
        self, inbound, *, kind=None, excluding_ids=()
    ) -> ViewMessage | None:
        excluded = set(excluding_ids or ())
        if inbound is None:
            return None
        wanted_kind = str(kind or "").strip().casefold()

        def carries_kind(item):
            if not wanted_kind:
                return item.has_media
            return any(
                str(media.get("kind") or media.get("type") or "").casefold()
                == wanted_kind
                for media in item.media
            )

        return next((
            item for item in self.messages
            if item.is_outbound and item.is_after(inbound)
            and carries_kind(item) and item.id not in excluded
        ), None)

    def find(self, message_id) -> ViewMessage | None:
        wanted = str(message_id or "").strip()
        if not wanted:
            return None
        return next((item for item in self.messages if item.id == wanted), None)

    def already_replied(self, inbound=None) -> bool:
        """True when an outbound message follows the (latest) inbound one."""
        target = inbound if inbound is not None else self.latest_inbound
        return self.outbound_after(target) is not None

    def as_history(self) -> list[dict]:
        return [
            {
                "id": item.id,
                "text": item.text,
                "direction": item.direction,
                "sender": item.sender,
                "timestamp": item.timestamp,
                "index": item.order,
                "media": list(item.media),
            }
            for item in self.messages
        ]


def direction_debug_block(message) -> str:
    """The DOM evidence about a bubble whose direction could not be resolved.

    One formatted block, shared by everything that reports the failure, so the
    parser can be fixed from the log alone instead of from a screenshot.
    """
    get = message.get if isinstance(message, dict) else (
        lambda key, default=None, source=message: getattr(source, key, default)
    )
    debug = get("direction_debug") or {}
    fields = (
        ("tag", debug.get("tag")),
        ("class", debug.get("class")),
        ("data-e2e", debug.get("data_e2e")),
        ("parent_tag", debug.get("parent_tag")),
        ("parent_class", debug.get("parent_class")),
        ("parent_data-e2e", debug.get("parent_data_e2e")),
        ("rect_left", debug.get("rect_left")),
        ("rect_right", debug.get("rect_right")),
        ("container_left", debug.get("container_left")),
        ("container_right", debug.get("container_right")),
        ("container_center", debug.get("container_center")),
        (
            "text_hash",
            content_fingerprint(debug.get("text") or normalize_text(get("text"))),
        ),
    )
    return "\n".join(
        f"{name}={'?' if value in (None, '') else value}" for name, value in fields
    )


def resolve_inbound(view: ConversationView, inbound_message_id=None) -> ViewMessage | None:
    """The inbound message a decision is about: the named one, else the latest."""
    return view.find(inbound_message_id) or view.latest_inbound

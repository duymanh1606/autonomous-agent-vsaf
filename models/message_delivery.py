import hashlib
from enum import Enum


class MessageSendState(str, Enum):
    """What the UI proves about one send attempt."""

    SENT = "SENT"
    NOT_SENT = "NOT_SENT"
    UNCERTAIN = "UNCERTAIN"


class SendTaskState(str, Enum):
    """Lifecycle of one deduplicated send task.

    Only `SENT` and a fresh `IN_FLIGHT` block a new attempt. Everything else is
    a finished-but-unsuccessful attempt, so the same reply may be retried; a
    stale signature must never silence a message the customer never got.
    """

    IN_FLIGHT = "IN_FLIGHT"
    SENT = "SENT"
    FAILED = "FAILED"
    ABORTED = "ABORTED"
    EXPIRED = "EXPIRED"

    @property
    def blocks_send(self) -> bool:
        return self in {SendTaskState.SENT, SendTaskState.IN_FLIGHT}

    @property
    def allows_retry(self) -> bool:
        return self in {
            SendTaskState.FAILED, SendTaskState.ABORTED, SendTaskState.EXPIRED
        }


RETRYABLE_SEND_STATES = frozenset(
    state.value for state in SendTaskState if state.allows_retry
)


class SendClaimOutcome(str, Enum):
    GRANTED = "GRANTED"
    ALREADY_SENT = "ALREADY_SENT"
    IN_FLIGHT_BY_OTHER = "IN_FLIGHT_BY_OTHER"
    EXPIRED_REQUIRES_RECONCILIATION = "EXPIRED_REQUIRES_RECONCILIATION"


class ComposerState(str, Enum):
    EMPTY = "EMPTY"
    HAS_TEXT = "HAS_TEXT"
    UNKNOWN = "UNKNOWN"


def normalize_conversation_id(conversation_id) -> str:
    return str(conversation_id or "").strip().casefold().lstrip("@")


def reply_text_hash(text) -> str:
    """Hash of the exact approved reply text, whitespace-normalized."""
    normalized = " ".join(str(text or "").split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def message_signature(
    platform, conversation_id, inbound_message_id, text,
    *, inbound_watermark="", attachment_hashes=(),
) -> str:
    """Identity of one reply: `platform + conversation + inbound + reply text`.

    The task id is deliberately absent. Two tasks answering the same inbound
    message with the same text are the same send, so a retry after a crash
    reuses the signature instead of creating a second, invisible one. What the
    signature must never decide on its own is whether the reply was delivered -
    only the open conversation can answer that.
    """
    normalized = "\0".join((
        str(platform or "").strip().casefold(),
        normalize_conversation_id(conversation_id),
        str(inbound_message_id or "").strip(),
        str(inbound_watermark or "").strip(),
        reply_text_hash(text),
        *sorted(str(value or "").strip().casefold() for value in attachment_hashes),
    ))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

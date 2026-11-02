"""The one place where "a human must take over" becomes visible.

Three things have to happen when the agent gives a conversation up, and until
now each of them lived somewhere else - or nowhere at all:

* a row in `message_escalations`, so the case survives a restart and can be
  worked through later;
* an `escalation.opened` event, which is what makes the dashboard raise a
  notification the operator actually sees;
* a log line, for whoever is reading the terminal.

A since-removed queue-driven reply path did the first, the since-retired
extension runtime did the second, and the inbox scan skill - the path that
actually answers customers - did only the third. A customer the agent could not answer was therefore dropped
in silence: nothing on the dashboard, nothing in the database, one warning in a
log nobody was watching.

`HumanHandoff` does all three from one call, and it degrades instead of
failing: without a database it still publishes and logs, without an event bus it
still records. Reporting a problem must never become a second problem.
"""

import logging
from datetime import datetime, timezone

from models.customer_state import CustomerState
from models.message_event import MessageEvent


logger = logging.getLogger(__name__)


class HumanHandoff:
    def __init__(
        self,
        *,
        account_id: str = "",
        escalation_service=None,
        platform: str = "tiktok_web",
        events=None,
        conversation_control=None,
    ):
        self.account_id = account_id or ""
        self.escalation_service = escalation_service
        self.platform = platform
        # Attached after construction by the runtime that owns the control
        # server, exactly like `inbox_skill.reply_provider`: the event bus only
        # exists once the control plane has started.
        self.events = events
        # Whether the agent or a human currently owns each conversation.
        self.conversation_control = conversation_control

    async def open(
        self,
        *,
        conversation_id: str,
        reason: str,
        conversation_name: str = "",
        message=None,
        analysis=None,
    ) -> None:
        """Record, announce and log one conversation that needs a person.

        `message` is the customer message the agent stopped on, when there is
        one; a thread that could not be read at all has none, and the handoff
        still has to happen - "the agent cannot read this thread" is exactly the
        case a human needs to see.
        """
        conversation_id = str(conversation_id or conversation_name or "").strip()
        if not conversation_id:
            logger.warning("[handoff] refused: no conversation to attribute it to")
            return

        logger.warning(
            "[handoff] a human must take over conversation=%s reason=%s message=%s",
            conversation_name or conversation_id, reason,
            getattr(message, "id", None) or "none",
        )
        await self._record(conversation_id, conversation_name, reason, message, analysis)
        if self.conversation_control is not None:
            # A manual takeover that already happened must not be downgraded
            # back to WAITING_HUMAN by a late escalation - `wait_for_human`
            # itself preserves HUMAN.
            await self.conversation_control.wait_for_human(conversation_id, reason)
        self._announce(conversation_id, conversation_name, reason, message)

    # ------------------------------------------------------------- database
    async def _record(self, conversation_id, conversation_name, reason, message, analysis):
        if self.escalation_service is None:
            return
        try:
            await self.escalation_service.escalate(
                self._event(conversation_id, conversation_name, message),
                CustomerState(
                    conversation_id=conversation_id,
                    customer_id=conversation_id,
                    stage="human_required",
                    requires_human=True,
                ),
                reason,
                analysis,
            )
        except Exception:
            # The operator still gets the event and the log line; losing the row
            # is not a reason to fail the scan that reported the problem.
            logger.exception(
                "[handoff] recording the escalation failed conversation=%s",
                conversation_id,
            )

    def _event(self, conversation_id, conversation_name, message) -> MessageEvent:
        """The inbound message as an event, so the row says what was stopped on.

        The escalation table keys on `event_id`, and `UNIQUE(account_id,
        event_id, reason)` is what stops one unanswerable thread from filling
        the table on every scan: the same message escalated for the same reason
        stays one row.
        """
        message_id = str(getattr(message, "id", "") or "").strip()
        text = str(getattr(message, "text", "") or "").strip()
        return MessageEvent(
            id=f"handoff-{conversation_id}-{message_id or 'unreadable'}",
            account_id=self.account_id,
            platform=self.platform,
            conversation_id=conversation_id,
            customer_id=conversation_id,
            customer_name=conversation_name or None,
            message_id=message_id or f"{conversation_id}-unreadable",
            # `message` has a minimum length, and a thread the agent could not
            # read has no text to put here.
            message=text or "(agent could not read this thread)",
            timestamp=datetime.now(timezone.utc),
        )

    # ---------------------------------------------------------------- events
    def _announce(self, conversation_id, conversation_name, reason, message):
        publish = getattr(self.events, "publish", None)
        if publish is None:
            return
        try:
            publish(
                "escalation.opened",
                conversation=conversation_name or conversation_id,
                conversation_id=conversation_id,
                reason=reason,
                message_id=getattr(message, "id", None),
                text=(str(getattr(message, "text", "") or "").strip() or None),
            )
        except Exception:
            logger.exception("[handoff] publishing the escalation event failed")

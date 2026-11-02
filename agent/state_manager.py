from datetime import datetime, timezone

from models.analysis import MessageAnalysis
from models.customer_state import CustomerState
from models.message_event import MessageEvent, MessageEventStatus


class CustomerStateManager:
    """The only component allowed to calculate customer-state transitions."""

    def __init__(self, registered_stages):
        self.registered_stages = frozenset(registered_stages)

    def new(self, event: MessageEvent) -> CustomerState:
        return CustomerState(
            customer_id=event.customer_id,
            conversation_id=event.conversation_id,
        )

    def transition(
        self, state: CustomerState, event: MessageEvent, analysis: MessageAnalysis
    ) -> CustomerState:
        next_stage = analysis.next_stage
        if next_stage not in self.registered_stages:
            next_stage = "human_required"
        entities = analysis.entities
        return state.model_copy(update={
            "intent": analysis.intent,
            "stage": next_stage,
            "product_id": entities.product_id or state.product_id,
            "product_name": entities.product_name or state.product_name,
            "size": entities.size or state.size,
            "quantity": entities.quantity or state.quantity,
            "sentiment": analysis.sentiment,
            "requires_tool": analysis.requires_tool,
            "required_tool": analysis.tool_name,
            "requires_human": analysis.requires_human,
            "last_message": event.message,
            "message_count": state.message_count + 1,
            "unknown_intent_count": (
                state.unknown_intent_count + 1
                if analysis.intent == "unknown" else 0
            ),
            "updated_at": datetime.now(timezone.utc),
        })

    def require_human(self, state: CustomerState) -> CustomerState:
        return state.model_copy(update={
            "stage": "human_required",
            "requires_human": True,
            "updated_at": datetime.now(timezone.utc),
        })

    def mark_event(
        self, state: CustomerState, event: MessageEvent, status: MessageEventStatus
    ) -> CustomerState:
        metadata = dict(state.metadata)
        metadata.update({
            "last_processed_event_id": event.id,
            "last_processed_event_status": status.value,
        })
        return state.model_copy(update={
            "metadata": metadata,
            "updated_at": datetime.now(timezone.utc),
        })

    def update_summary(self, state: CustomerState, summary: str) -> CustomerState:
        return state.model_copy(update={
            "conversation_summary": summary,
            "updated_at": datetime.now(timezone.utc),
        })

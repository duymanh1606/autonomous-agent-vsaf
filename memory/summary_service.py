import json

from llm.base import LLMProvider
from models.conversation import ConversationMessage
from models.customer_state import CustomerState
from models.message_errors import MessageErrorType, MessageProcessingError


class ConversationSummaryService:
    def __init__(self, llm: LLMProvider, summarize_after_messages=20, source_limit=40):
        self.llm = llm
        self.summarize_after_messages = max(1, int(summarize_after_messages))
        self.source_limit = max(1, int(source_limit))

    def should_summarize(self, state: CustomerState) -> bool:
        return state.message_count > 0 and (
            state.message_count % self.summarize_after_messages == 0
        )

    async def summarize(
        self, state: CustomerState, messages: list[ConversationMessage]
    ) -> str:
        compact = [message.model_dump(mode="json") for message in messages[-self.source_limit:]]
        prompt = (
            "Summarize only durable customer-service facts from this conversation. "
            "Do not infer missing facts. Keep product, size, quantity, unresolved request, "
            "sentiment and commitments; omit greetings and sensitive secrets.\n"
            f"Previous summary: {json.dumps(state.conversation_summary, ensure_ascii=False)}\n"
            f"Messages: {json.dumps(compact, ensure_ascii=False)}"
        )
        try:
            return (await self.llm.generate_text(prompt)).strip()
        except Exception as exc:
            raise MessageProcessingError(
                MessageErrorType.LLM_ERROR, "Conversation summary failed"
            ) from exc

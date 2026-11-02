from enum import Enum

from models.analysis import MessageAnalysis


class MessageRoute(str, Enum):
    DIRECT = "DIRECT"
    TOOL = "TOOL"
    STRONG_LLM = "STRONG_LLM"
    HUMAN = "HUMAN"


class MessageRouter:
    def __init__(self, strong_llm_threshold=0.65, human_intents=()):
        self.strong_llm_threshold = float(strong_llm_threshold)
        self.human_intents = frozenset(human_intents)

    def route(self, analysis: MessageAnalysis) -> MessageRoute:
        if analysis.requires_human or analysis.intent in self.human_intents:
            return MessageRoute.HUMAN
        if analysis.requires_tool:
            return MessageRoute.TOOL
        if analysis.requires_strong_llm or analysis.confidence < self.strong_llm_threshold:
            return MessageRoute.STRONG_LLM
        return MessageRoute.DIRECT

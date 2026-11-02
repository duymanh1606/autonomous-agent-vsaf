from enum import Enum


class MessageErrorType(str, Enum):
    LLM_ERROR = "LLM_ERROR"
    TOOL_ERROR = "TOOL_ERROR"
    TEMPORARY_ERROR = "TEMPORARY_ERROR"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_RATE_LIMITED = "AUTH_RATE_LIMITED"
    SECURITY_CHALLENGE = "SECURITY_CHALLENGE"
    UI_ERROR = "UI_ERROR"


class MessageProcessingError(RuntimeError):
    def __init__(self, error_type: MessageErrorType, message: str):
        super().__init__(message)
        self.error_type = error_type

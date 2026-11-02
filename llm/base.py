from abc import ABC, abstractmethod
from typing import TypeVar

from pydantic import BaseModel


StructuredModel = TypeVar("StructuredModel", bound=BaseModel)


class LLMProvider(ABC):
    """Provider-neutral interface. Providers receive prompts, never UI handles."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    async def generate_structured(
        self, prompt: str, response_model: type[StructuredModel]
    ) -> StructuredModel:
        raise NotImplementedError

    @abstractmethod
    async def generate_text(self, prompt: str) -> str:
        raise NotImplementedError

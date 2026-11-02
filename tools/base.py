from abc import ABC, abstractmethod


class BaseTool(ABC):

    name: str

    @abstractmethod
    async def execute(self, **kwargs):
        raise NotImplementedError


class BusinessTool(ABC):
    """Read/write business capability isolated from browser automation."""

    name: str

    @abstractmethod
    async def execute(self, arguments: dict):
        raise NotImplementedError

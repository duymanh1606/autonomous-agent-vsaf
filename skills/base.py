from abc import ABC, abstractmethod


class Skill(ABC):

    name: str

    capabilities: list[str]

    @abstractmethod
    async def can_handle(
        self,
        task,
        context
    ):
        raise NotImplementedError

    @abstractmethod
    async def execute(
        self,
        task,
        context
    ):
        raise NotImplementedError

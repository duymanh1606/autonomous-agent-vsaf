from abc import ABC, abstractmethod


class PlatformAdapter(ABC):
    @abstractmethod
    def next_action(self, task, observation):
        raise NotImplementedError

    @abstractmethod
    def task_complete(self, task, observation):
        raise NotImplementedError

    @abstractmethod
    def verify(self, action, before_screen, observation):
        raise NotImplementedError

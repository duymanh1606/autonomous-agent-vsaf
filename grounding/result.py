from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GroundingResult:
    x: int
    y: int
    source: str
    confidence: float
    target: str
    target_id: str | None = None
    expected_state: str | None = None
    viewport: tuple[int, int] | None = None
    observation: Any = None
    dom_target: Any = None

    def as_dict(self):
        return {
            "x": self.x,
            "y": self.y,
            "source": self.source,
            "confidence": self.confidence,
            "target": self.target,
        }

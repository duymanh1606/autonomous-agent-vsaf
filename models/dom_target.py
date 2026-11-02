from dataclasses import dataclass
from typing import Any


@dataclass
class DomTarget:
    name: str
    visible: bool
    clickable: bool
    bbox: dict[str, float]
    click_point: dict[str, float]
    locator: Any = None
    selector: str | None = None
    text: str | None = None

    def as_dict(self):
        return {
            "name": self.name,
            "visible": self.visible,
            "clickable": self.clickable,
            "bbox": dict(self.bbox),
            "click_point": dict(self.click_point),
        }


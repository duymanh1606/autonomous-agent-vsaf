from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from grounding.result import GroundingResult
from models.auth import AuthState


DEFAULT_COORDINATE_CONFIG = (
    Path(__file__).resolve().parents[1] / "configs" / "ui_coordinates.yaml"
)


@dataclass(frozen=True)
class FixedCoordinateAction:
    target: str
    coordinate: tuple[int, int]
    viewport: tuple[int, int]
    expected_state: str | None = None


class CoordinateResolver:
    """Resolve reviewed fixed coordinates and enforce observation guards."""

    def __init__(self, config: dict[str, Any], confidence_threshold: float = 0.80):
        viewport = config.get("base_viewport") or {}
        self.base_width = self._positive_int(viewport.get("width"), "base_viewport.width")
        self.base_height = self._positive_int(viewport.get("height"), "base_viewport.height")
        self.coordinates = config.get("coordinates") or {}
        self.actions = config.get("actions") or {}
        self.confidence_threshold = float(confidence_threshold)

    @classmethod
    def from_file(
        cls,
        path: str | Path = DEFAULT_COORDINATE_CONFIG,
        confidence_threshold: float = 0.80,
    ):
        with Path(path).open("r", encoding="utf-8") as file:
            config = yaml.safe_load(file) or {}
        return cls(config, confidence_threshold=confidence_threshold)

    def resolve(self, state, target, viewport):
        state_key = self.normalize_state(state)
        target_key = str(target or "").strip().casefold()
        point = (self.coordinates.get(state_key) or {}).get(target_key)
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            return None
        try:
            width = self._positive_int(viewport[0], "viewport.width")
            height = self._positive_int(viewport[1], "viewport.height")
            base_x, base_y = float(point[0]), float(point[1])
        except (IndexError, TypeError, ValueError):
            return None
        x = round(base_x * width / self.base_width)
        y = round(base_y * height / self.base_height)
        return (x, y) if 0 <= x < width and 0 <= y < height else None

    def resolve_grounding(self, state, target, observation, expected_state=None):
        if not self.observation_is_eligible(observation):
            return None
        viewport = observation.metadata.get("viewport") or {}
        size = (viewport.get("width"), viewport.get("height"))
        coordinate = self.resolve(state, target, size)
        if coordinate is None:
            return None
        return GroundingResult(
            x=coordinate[0],
            y=coordinate[1],
            source="fixed_coordinate",
            confidence=1.0,
            target=str(target).strip().casefold(),
            expected_state=self.normalize_state(expected_state) or None,
            viewport=(int(size[0]), int(size[1])),
        )

    def route_for_action(self, action_name):
        return dict(self.actions.get(action_name) or {})

    def resolve_action(self, action_name, observation):
        route = self.route_for_action(action_name)
        target = str(route.get("target") or "").strip().casefold()
        if not target:
            return None
        result = self.resolve_grounding(
            observation.screen,
            target,
            observation,
            expected_state=route.get("expected_state"),
        )
        if result is None:
            return None
        return FixedCoordinateAction(
            target=result.target,
            coordinate=(result.x, result.y),
            viewport=result.viewport,
            expected_state=result.expected_state,
        )

    def target_for_action(self, action_name):
        target = self.route_for_action(action_name).get("target")
        return str(target).strip().casefold() if target else None

    def observation_is_eligible(self, observation):
        if observation is None:
            return False
        metadata = observation.metadata or {}
        viewport = metadata.get("viewport") or {}
        return bool(
            not observation.unknown
            and observation.screen
            and observation.screen_confidence >= self.confidence_threshold
            and observation.auth_state == AuthState.LOGGED_IN
            and not observation.security_challenge
            and not metadata.get("blocking_overlay_present", False)
            and not metadata.get("ui_abnormal", True)
            and viewport.get("width")
            and viewport.get("height")
        )

    @staticmethod
    def normalize_state(state):
        value = str(state or "").strip().casefold()
        if value.startswith("tiktok_"):
            value = value[len("tiktok_"):]
        return {
            "video_feed": "feed",
            "inbox": "messages",
            "search_results": "search",
        }.get(value, value)

    @staticmethod
    def _positive_int(value, name):
        number = int(value)
        if number <= 0:
            raise ValueError(f"{name} must be positive")
        return number

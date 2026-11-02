import asyncio
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


class GuiDatasetCollector:
    """Persist reviewable GUI action traces without treating predictions as GT."""

    def __init__(self, root="data/gui_training", platform="tiktok_web", enabled=True):
        self.root = Path(root)
        self.platform = platform
        self.enabled = bool(enabled)
        self.images_dir = self.root / "images"
        self.successful_dir = self.root / "successful"
        self.failed_dir = self.root / "failed"
        self.actions_path = self.root / "actions.jsonl"
        self._lock = asyncio.Lock()
        self._seen_layouts = set()
        self._seen_samples = set()
        self._stored_images = {}
        self._target_positions = {}

    async def record(
        self,
        *,
        screen_state,
        target,
        instruction,
        source,
        predicted_point,
        clicked_point,
        viewport,
        success,
        next_state,
        expected_state=None,
        confidence=None,
        screenshot_path=None,
        observation=None,
    ):
        if not self.enabled:
            return
        state = self._normalize_state(screen_state)
        next_state = self._normalize_state(next_state)
        expected_state = self._normalize_state(expected_state)
        metadata = (observation.metadata if observation else {}) or {}
        layout = str(metadata.get("signature") or "")
        layout_key = (state, str(target), layout, tuple(viewport or ()))
        new_layout = bool(layout and layout_key not in self._seen_layouts)
        self._seen_layouts.add(layout_key)
        point = tuple(clicked_point or predicted_point or ())
        position_key = (state, str(target), tuple(viewport or ()))
        previous_positions = self._target_positions.setdefault(position_key, set())
        position_changed = bool(point and previous_positions and point not in previous_positions)
        if point:
            previous_positions.add(point)
        unusual_ui = bool(
            metadata.get("blocking_overlay_present")
            or metadata.get("ui_abnormal")
            or getattr(observation, "unknown", False)
        )
        useful_image = bool(
            source == "gemini"
            or (source == "fixed_coordinate" and not success)
            or new_layout
            or position_changed
            or unusual_ui
        )
        sample_key = (
            state, str(target), str(source), layout,
            tuple(clicked_point or ()), bool(success), next_state,
        )
        if sample_key in self._seen_samples and not unusual_ui:
            useful_image = False
        self._seen_samples.add(sample_key)

        stored_screenshot = None
        if useful_image and screenshot_path:
            stored_screenshot = await self._store_image(
                Path(screenshot_path), success=bool(success)
            )

        gemini_verified = bool(
            source == "gemini"
            and success
            and expected_state
            and next_state == expected_state
        )
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "platform": self.platform,
            "screen_state": state,
            "target": str(target),
            "instruction": str(instruction),
            "source": str(source),
            "screenshot": stored_screenshot,
            "predicted_point": list(predicted_point) if predicted_point else None,
            "clicked_point": list(clicked_point) if clicked_point else None,
            "viewport": list(viewport) if viewport else None,
            "confidence": confidence,
            "success": bool(success),
            "expected_state": expected_state or None,
            "next_state": next_state,
            "label_source": "gemini_pseudo_label" if gemini_verified else None,
            "review_status": "pending",
            "bbox_gt": None,
        }
        await self._append(payload)

    async def _store_image(self, source_path, success):
        if not source_path.is_file():
            return None
        source_key = str(source_path.resolve())
        if source_key in self._stored_images:
            relative = self._stored_images[source_key]
            stored = self.root / relative
            review_dir = self.successful_dir if success else self.failed_dir
            review_copy = review_dir / stored.name
            if stored.is_file() and not review_copy.exists():
                async with self._lock:
                    await asyncio.to_thread(self._ensure_directories)
                    await asyncio.to_thread(shutil.copy2, stored, review_copy)
            return relative
        suffix = source_path.suffix.lower() or ".png"
        digest = await asyncio.to_thread(self._file_digest, source_path)
        name = f"{digest}{suffix}"
        destination = self.images_dir / name
        review_dir = self.successful_dir if success else self.failed_dir
        review_copy = review_dir / name
        async with self._lock:
            await asyncio.to_thread(self._ensure_directories)
            if not destination.exists():
                await asyncio.to_thread(shutil.copy2, source_path, destination)
            if not review_copy.exists():
                await asyncio.to_thread(shutil.copy2, destination, review_copy)
        relative = str(destination.relative_to(self.root))
        self._stored_images[source_key] = relative
        return relative

    async def _append(self, payload):
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        async with self._lock:
            await asyncio.to_thread(self._ensure_directories)
            await asyncio.to_thread(self._append_line, line)

    def _ensure_directories(self):
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.successful_dir.mkdir(parents=True, exist_ok=True)
        self.failed_dir.mkdir(parents=True, exist_ok=True)

    def _append_line(self, line):
        with self.actions_path.open("a", encoding="utf-8") as file:
            file.write(line)

    @staticmethod
    def _file_digest(path):
        digest = hashlib.sha256()
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _normalize_state(state):
        value = str(state or "unknown").strip().casefold()
        return value.removeprefix("tiktok_")

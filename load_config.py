import copy
import logging
import os
from pathlib import Path
from typing import Any

import yaml


logger = logging.getLogger(__name__)


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "configs" / "config.yaml"
DEFAULT_ENV_PATH = BASE_DIR / ".env"


def load_env_file(env_path: str | Path | None = None) -> list[str]:
    """Load `KEY=value` lines from `.env` into the environment.

    API keys are read from the environment only, never from YAML, so without
    this the documented `.env` file would be inert and every credential-backed
    feature would silently disable itself. A variable that is already exported
    always wins: the real environment outranks the file.

    Returns the names that were applied, so callers can log them without ever
    touching the values.
    """
    # AGENT_ENV_FILE lets a CLI or a test point at a different file without
    # touching the repo's own .env - the real environment still outranks
    # whatever that file says, same as always.
    path = Path(env_path or os.environ.get("AGENT_ENV_FILE") or DEFAULT_ENV_PATH)
    if not path.exists():
        return []

    applied = []
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#") or "=" not in entry:
            continue
        name, _, value = entry.partition("=")
        name = name.strip()
        if name.startswith("export "):
            name = name[len("export "):].strip()
        if not name or name in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not value:
            continue
        os.environ[name] = value
        applied.append(name)
    return applied


# A workspace config is an *overlay*: it names the shared config it builds on
# and holds only what differs from it. Reading merges the two, so a change to
# configs/config.yaml reaches every workspace; saving writes the overlay alone,
# so a workspace never freezes a copy of the shared defaults again. A YAML
# without `extends` is read as a complete config, as before.
EXTENDS_KEY = "extends"
# dashboard_api/account_config.py: which config migrations this overlay has had.
VERSION_KEY = "config_version"

# Overlay paths that legitimately do not exist in the shared config: they are
# written per workspace by dashboard_api/accounts.py (`render_config`).
_ACCOUNT_ONLY_PATHS = (
    ("egw",), ("llm", "api_key"), ("account", "index"), ("control",),
    ("grounding",),
)


def deep_merge(base: dict, overlay: dict) -> dict:
    """`overlay` on top of `base`: mappings merge key by key, anything else
    (a list, a scalar, null) replaces. Neither argument is modified."""
    merged = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def unknown_paths(overlay: dict, base: dict, prefix=()) -> list[str]:
    """Overlay leaves the shared config does not know: a typo, or a setting
    whose feature was removed - either way nothing reads it."""
    found = []
    for key, value in overlay.items():
        path = (*prefix, key)
        if any(path[:len(owned)] == owned for owned in _ACCOUNT_ONLY_PATHS):
            continue
        if not isinstance(base, dict) or key not in base:
            found.append(".".join(str(part) for part in path))
        elif isinstance(value, dict) and isinstance(base[key], dict):
            found.extend(unknown_paths(value, base[key], path))
    return found


class Config:
    def __init__(self, data: dict[str, Any], path: Path | None = None, *,
                 overlay: dict[str, Any] | None = None,
                 header: dict[str, Any] | None = None):
        # The merged view every reader sees.
        self._data = data
        # Where this config was loaded from, so `save()` writes back to the
        # right account file instead of guessing the default.
        self.path = path
        # Set for a workspace overlay: what `save()` writes, alongside
        # `header` (`extends`, `config_version`).
        self._overlay = overlay
        self._header = dict(header or {})

    @property
    def is_overlay(self) -> bool:
        return self._overlay is not None

    def get(self, key: str, default=None):
        value: Any = self._data

        for part in key.split("."):
            if not isinstance(value, dict):
                return default

            value = value.get(part)

            if value is None:
                return default

        return value

    def __getitem__(self, key: str):
        return self._data[key]

    def set(self, key: str, value: Any) -> None:
        """Set a dotted key, creating intermediate dicts as needed.

        This only changes the in-memory tree; call `save()` to persist. Nothing
        in the running agent observes the change on its own - see
        `control/settings.py` for which values are also applied live.
        """
        parts = key.split(".")
        trees = [self._data]
        if self._overlay is not None:
            # Recorded in the overlay too: a value set for this workspace is
            # kept for it even if the shared default later changes.
            trees.append(self._overlay)
        for tree in trees:
            target = tree
            for part in parts[:-1]:
                existing = target.get(part)
                if not isinstance(existing, dict):
                    existing = {}
                    target[part] = existing
                target = existing
            target[parts[-1]] = copy.deepcopy(value)

    def save(self, path: str | Path | None = None) -> Path:
        """Write the config back atomically.

        A partially written YAML would make the agent unstartable, so the new
        content goes to a temp file in the same directory and is then renamed
        over the original - a rename is atomic within one filesystem.
        """
        destination = Path(path) if path else self.path
        if destination is None:
            raise ValueError("this config has no path to save to")
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = (
            {**self._header, **self._overlay} if self._overlay is not None else self._data
        )
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            yaml.safe_dump(
                content, stream, allow_unicode=True, sort_keys=False, indent=2
            )
        if destination.exists():
            # An account file may carry an API key or egw token (0600).
            os.chmod(temporary, destination.stat().st_mode & 0o777)
        os.replace(temporary, destination)
        return destination

    @property
    def raw(self) -> dict[str, Any]:
        return self._data

    def validate(self) -> None:
        """Fail fast for unsafe inbox/media values instead of coercing silently."""
        def number(key, *, minimum=None, maximum=None, integer=False):
            value = self.get(key)
            if value is None:
                return None
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{key} must be a number")
            if integer and int(value) != value:
                raise ValueError(f"{key} must be an integer")
            if minimum is not None and value < minimum:
                raise ValueError(f"{key} must be >= {minimum}")
            if maximum is not None and value > maximum:
                raise ValueError(f"{key} must be <= {maximum}")
            return value

        slots = number(
            "messaging.auto_check.max_conversations_per_scan",
            minimum=1, maximum=20, integer=True,
        )
        reservation = number(
            "messaging.auto_check.request_slot_reservation",
            minimum=0, maximum=20, integer=True,
        )
        if slots is not None and reservation is not None and reservation > slots:
            raise ValueError(
                "messaging.auto_check.request_slot_reservation cannot exceed "
                "max_conversations_per_scan"
            )
        quiet = number(
            "messaging.auto_check.reply_quiet_period_seconds", minimum=0
        )
        burst = number(
            "messaging.auto_check.max_burst_wait_seconds", minimum=0
        )
        if burst is None:
            burst = number(
                "messaging.auto_check.reply_wait_timeout_seconds", minimum=0
            )
        if quiet is not None and burst is not None and burst <= quiet:
            raise ValueError(
                "messaging.auto_check.max_burst_wait_seconds must be greater than "
                "reply_quiet_period_seconds"
            )
        number("messaging.task_lease_seconds", minimum=10, integer=True)
        number("messaging.send_lease_seconds", minimum=10, integer=True)
        number("messaging.auto_check.max_virtual_scroll_pages", minimum=1, maximum=100, integer=True)
        number("messaging.media.max_image_size_mb", minimum=1)
        number("messaging.media.max_video_size_mb", minimum=1)
        number("messaging.media.max_dm_video_duration_seconds", minimum=1, maximum=60)
        number("messaging.media.max_attachments", minimum=1, maximum=10, integer=True)
        number("auth.auto_login.max_attempts_per_process", minimum=1, maximum=5, integer=True)
        number("auth.auto_login.cooldown_seconds", minimum=30)
        number("auth.auto_login.post_submit_wait_seconds", minimum=0, maximum=30)
        number("auth.auto_login.fedcm_settle_seconds", minimum=0, maximum=30)
        mimes = self.get("messaging.media.allowed_mime_types")
        if mimes is not None and (
            not isinstance(mimes, list)
            or not mimes
            or any(not isinstance(value, str) or "/" not in value for value in mimes)
        ):
            raise ValueError("messaging.media.allowed_mime_types must be a non-empty MIME list")


def read_yaml(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def base_path_of(path: Path, data: dict[str, Any]) -> Path | None:
    """The shared config an overlay builds on, or None for a complete file."""
    extends = data.get(EXTENDS_KEY)
    if not extends:
        return None
    return (path.parent / str(extends)).resolve()


def load_config(
    config_path: str | Path | None = None,
) -> Config:

    load_env_file()

    path = (
        Path(config_path)
        if config_path
        else DEFAULT_CONFIG_PATH
    )

    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}"
        )

    data = read_yaml(path)
    base_path = base_path_of(path, data)
    if base_path is None:
        config = Config(data, path)
    else:
        if not base_path.exists():
            raise FileNotFoundError(f"Config {path} extends a missing file: {base_path}")
        base = read_yaml(base_path)
        if base_path_of(base_path, base) is not None:
            # One level only: a chain would make "where does this value come
            # from" a search instead of a glance at two files.
            raise ValueError(f"{base_path} is itself an overlay; overlays cannot chain")
        header = {key: data.pop(key) for key in (EXTENDS_KEY, VERSION_KEY) if key in data}
        for unknown in unknown_paths(data, base):
            logger.warning("[config] %s: %s is not a known setting; nothing reads it",
                           path.name, unknown)
        config = Config(deep_merge(base, data), path, overlay=data, header=header)
    config.validate()
    return config

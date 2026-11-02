"""Workspace config overlays: versioned migrations and the legacy conversion.

A workspace YAML (configs/accounts/<id>.yaml) holds only what differs from the
shared configs/config.yaml - see `load_config.EXTENDS_KEY`. That makes a change
to the shared file reach every workspace, which is the point, and also the
risk: changing a default there changes behaviour for workspaces that were
created under the old one.

When that is not wanted, the change ships with a **config migration** here:
a function that writes the old value into the overlay of every workspace
created before it, so only new workspaces get the new default. Each overlay
records `config_version`; a workspace created now starts at
CURRENT_CONFIG_VERSION and never runs a migration written before it existed.

    1. Myanmar became the default reply language (configs/config.yaml); every
       workspace that existed before keeps Vietnamese.
    2. Gemini models stop being pinned per workspace: the copies written into
       each file are removed so configs/config.yaml decides for all.

Adding one: append a function, never edit or reorder an existing one - a
migration runs once per overlay, ever, so changing it later reaches nobody.
"""

import copy

from control.settings import HOT_APPLIABLE, RESTART_REQUIRED
from load_config import EXTENDS_KEY, VERSION_KEY


def get_path(tree: dict, dotted: str):
    value = tree
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def set_path(tree: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    for part in parts[:-1]:
        nested = tree.get(part)
        if not isinstance(nested, dict):
            nested = {}
            tree[part] = nested
        tree = nested
    tree[parts[-1]] = copy.deepcopy(value)


def _pin_vietnamese_reply_language(overlay: dict) -> None:
    if get_path(overlay, "messaging.reply_language") is None:
        set_path(overlay, "messaging.reply_language", "vi")


def _drop_pinned_models(overlay: dict) -> None:
    """Gemini models used to be pinned into every workspace's file, so a
    model change in configs/config.yaml reached no workspace. They were never
    an operator setting (every write overwrote them); removing them lets the
    shared config decide again."""
    for path in ("llm.model", "ai.planner.model", "vision.gemini.model"):
        parts = path.split(".")
        parents = [overlay]
        for part in parts[:-1]:
            node = parents[-1].get(part)
            if not isinstance(node, dict):
                break
            parents.append(node)
        else:
            parents[-1].pop(parts[-1], None)
            # Leave no empty sections behind (`vision: {gemini: {}}`).
            for depth in range(len(parents) - 1, 0, -1):
                if not parents[depth]:
                    parents[depth - 1].pop(parts[depth - 1], None)


# (version, what it keeps from changing) - append only, see the module docstring.
CONFIG_MIGRATIONS = (
    (1, _pin_vietnamese_reply_language),
    (2, _drop_pinned_models),
)
CURRENT_CONFIG_VERSION = CONFIG_MIGRATIONS[-1][0]


def migrate(overlay: dict, version: int) -> int:
    """Apply every migration newer than `version`; returns the new version."""
    for number, migration in CONFIG_MIGRATIONS:
        if number > version:
            migration(overlay)
            version = number
    return version


# What an operator can change for one workspace from the dashboard - the only
# values of a legacy full copy that may be a deliberate choice rather than a
# frozen copy of an old default. Everything else is re-derived.
OPERATOR_SETTINGS = tuple(
    key for key in (*HOT_APPLIABLE, *RESTART_REQUIRED)
    if key not in ("browser.cdp_url", "storage.database_url")
)


def is_overlay(data: dict) -> bool:
    return bool(data.get(EXTENDS_KEY))


def overlay_from_legacy(legacy: dict, base: dict) -> dict:
    """The overlay equivalent of a legacy full copy, as far as it was chosen.

    Migrations run first, while a key's absence still means "this workspace
    predates that setting" - the condition they are written against. After
    that only operator settings that differ from the shared config are kept;
    every other value in the copy was a default frozen at creation time, and
    from now on follows the shared config. The workspace's own identity
    (id, ports, paths, keys) is re-applied by dashboard_api/accounts.py.
    """
    legacy = copy.deepcopy(legacy)
    migrate(legacy, 0)
    overlay: dict = {}
    for key in OPERATOR_SETTINGS:
        value = get_path(legacy, key)
        if value is not None and value != get_path(base, key):
            set_path(overlay, key, value)
    return overlay


def split_header(data: dict) -> tuple[dict, int]:
    """(overlay without its header, its config_version)."""
    overlay = {key: value for key, value in data.items()
               if key not in (EXTENDS_KEY, VERSION_KEY)}
    return overlay, int(data.get(VERSION_KEY) or 0)

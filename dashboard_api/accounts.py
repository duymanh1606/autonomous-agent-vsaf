"""One account = one Chrome profile = one config file = one set of ports.

Everything that would otherwise collide between two agents is derived from the
account's index here: CDP port, bridge port, control port, screenshot and log
directories. Getting one of these wrong is the failure mode that looks like
"multi-account works" while two accounts quietly share one brain, so they are
all derived in a single place.

The database is the deliberate exception: every account shares one PostgreSQL
database and is separated logically by `account_id`, enforced on every row and
in every query rather than by handing each account its own file.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
from dataclasses import dataclass, asdict
from pathlib import Path
from threading import RLock

import yaml

from dashboard_api import account_config
from dashboard_api.account_config import CURRENT_CONFIG_VERSION
from load_config import BASE_DIR, EXTENDS_KEY, VERSION_KEY, load_config, read_yaml


logger = logging.getLogger(__name__)

ACCOUNTS_DIR = BASE_DIR / "configs" / "accounts"
REGISTRY_PATH = ACCOUNTS_DIR / "accounts.json"
PROFILES_DIR = Path.home() / ".autonomous-agent-chrome"

# account_id becomes a YAML filename (`{id}.yaml`) and, together with
# profile_dir_name, a path segment under PROFILES_DIR/data/. Neither is
# sanitized anywhere else downstream, so this is the only gate against a
# value like "../../etc/x" escaping the intended directory.
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class InvalidAccountName(ValueError):
    """account_id or profile_dir_name failed the filesystem-safe name check."""


def _validate_name(value: str, field: str) -> None:
    if not _NAME_PATTERN.match(value):
        raise InvalidAccountName(
            f"{field} chỉ được chứa chữ, số, dấu - và _ (tối đa 64 ký tự), "
            f"không dấu cách hay ký tự đặc biệt: {value!r}"
        )

# Base ports; account index `i` shifts each by `i`. Overridable via .env
# (not hardcoded elsewhere) so a server that already has something else
# bound to one of these defaults can move the whole range without touching
# any source file - see deploy/README.md.
CDP_PORT = int(os.environ.get("AGENT_CDP_BASE_PORT", "29222"))
CONTROL_PORT = int(os.environ.get("AGENT_CONTROL_BASE_PORT", "28900"))
# Each workspace now gets its own Xvfb + x11vnc + noVNC instead of every
# workspace sharing one display - see dashboard_api/processes.py's
# AccountProcesses.start_chrome(). Bases are chosen clear of the OLD shared
# instance's fixed values (:99 / 25900 / 26080) so a server mid-migration
# (old static systemd units not yet torn down) can never collide with the
# new per-workspace ports even if the teardown step is skipped or reordered -
# see deploy/README.md's migration section.
DISPLAY_BASE = int(os.environ.get("AGENT_DISPLAY_BASE", "100"))
VNC_PORT = int(os.environ.get("AGENT_VNC_BASE_PORT", "25901"))
NOVNC_PORT = int(os.environ.get("AGENT_NOVNC_BASE_PORT", "26081"))


# egw runs on this same machine (its listeners are loopback-only), so the
# default needs no configuration. Only consulted for accounts with an
# egw_token; never makes a proxy appear for the others.
DEFAULT_EGW_PROXY_URL = "http://127.0.0.1:3128"


def egw_feature_enabled() -> bool:
    """Master switch for the egress gateway, read from the environment.

    Off (the default) means egw does not exist as far as this deployment is
    concerned: no `egw` YAML section, no `--proxy-server`, no CDP handler,
    feature hidden in the dashboard - even for accounts that still carry a
    token. Tokens and route assignments are kept, so flipping it back on
    needs no re-provisioning. Read at call time, not import time: a change
    takes effect at the next supervisor restart + workspace Start.
    """
    return os.environ.get("EGW_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Account:
    id: str
    label: str
    index: int
    # Empty means "derive from id" - kept optional so existing registry rows
    # (written before this field existed) still load.
    profile_dir_name: str = ""
    # Never returned to the frontend (see as_dict) - only used to write the
    # account's YAML and to inject GEMINI_API_KEY into its agent process.
    api_key: str = ""
    # egress-ip-gateway credential. Empty means this workspace does not use
    # egw at all: no proxy flag on its Chrome, no `egw` section in its YAML.
    # That emptiness is the only opt-in switch - EGW_* environment variables
    # alone never turn the proxy on. Like api_key: never returned to the
    # frontend, only written into the account's own YAML.
    egw_token: str = ""
    # Which egw route (public IP) this workspace was mapped to when the token
    # was provisioned through the Admin API, and that route's source IP. Not
    # secrets - shown in the dashboard. expected_ip lets the agent verify at
    # startup that its traffic really leaves through this exact IP; both stay
    # empty for a token pasted by hand, and the agent then falls back to
    # "observed IP differs from the host's own" (tools/browser.py).
    egw_route: str = ""
    egw_expected_ip: str = ""
    # Operator intent ("Giữ thức"): blocks the idle sweeper from putting this
    # workspace's Chrome to sleep, regardless of idle time. Persisted (unlike
    # the rest of the sleep/wake lifecycle, which is in-memory only in
    # dashboard_api/main.py) because it reflects a deliberate operator
    # decision that should survive a supervisor restart.
    pinned: bool = False
    # dashboard_api users.id of whoever created this workspace. None for rows
    # written before this field existed (legacy/admin-created), which the
    # dashboard treats as "no owner" rather than guessing one.
    owner_id: int | None = None

    @property
    def config_path(self) -> Path:
        return ACCOUNTS_DIR / f"{self.id}.yaml"

    @property
    def profile_dir(self) -> Path:
        return PROFILES_DIR / (self.profile_dir_name or self.id)

    @property
    def cdp_port(self) -> int:
        return CDP_PORT + self.index

    @property
    def control_port(self) -> int:
        return CONTROL_PORT + self.index

    @property
    def display_num(self) -> int:
        """This workspace's own X11 display number - dashboard_api-internal
        only (drives DISPLAY/XAUTHORITY for its Chrome and the argv of its
        own Xvfb/x11vnc). Never sent to the frontend; see as_dict()."""
        return DISPLAY_BASE + self.index

    @property
    def vnc_port(self) -> int:
        """Raw RFB port for this workspace's own x11vnc. Never sent to the
        frontend - noVNC is the browser-facing surface, this is what it
        bridges to. See as_dict()."""
        return VNC_PORT + self.index

    @property
    def novnc_port(self) -> int:
        """The one VNC-related value the frontend needs: where this
        workspace's own noVNC web client listens. See as_dict()."""
        return NOVNC_PORT + self.index

    # No per-account database: every account shares one PostgreSQL database
    # and is separated by `account_id`. Only files stay split per account.

    def as_dict(self) -> dict:
        data = asdict(self)
        # api_key must never reach the frontend/logs - only whether one is set.
        data.pop("api_key", None)
        data.pop("egw_token", None)
        data.update({
            "cdp_port": self.cdp_port,
            "control_port": self.control_port,
            # display_num/vnc_port deliberately excluded - dashboard_api's own
            # process-spawning concern (see the properties' docstrings), not
            # something the frontend has a legitimate use for.
            "novnc_port": self.novnc_port,
            "config_path": str(self.config_path),
            "profile_dir": str(self.profile_dir),
            "data_dir": f"data/accounts/{self.id}",
            "has_api_key": bool(self.api_key),
            "has_egw_token": bool(self.egw_token),
            # What the workspace will actually do at its next Start: token
            # present AND the master switch on.
            "egw_active": bool(self.egw_token) and egw_feature_enabled(),
        })
        return data


class AccountRegistry:
    """Accounts on disk, plus the per-account config files they need."""

    def __init__(self, base_config_path: Path | None = None):
        self.base_config_path = Path(base_config_path or BASE_DIR / "configs" / "config.yaml")
        ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
        self._accounts: dict[str, Account] = {}
        # Guards create() end-to-end (quota check callers also acquire this -
        # see create_account in main.py) so two concurrent requests can't both
        # read the same pre-write state and both pass a workspace-count cap.
        self.lock = RLock()
        self._load()

    # ---------------------------------------------------------- registry
    def _load(self) -> None:
        if not REGISTRY_PATH.exists():
            return
        try:
            rows = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("[accounts] registry unreadable (%s); starting empty",
                           type(exc).__name__)
            return
        for row in rows:
            account = Account(
                id=row["id"], label=row.get("label", row["id"]),
                index=int(row["index"]),
                profile_dir_name=row.get("profile_dir_name", ""),
                api_key=row.get("api_key", ""),
                egw_token=row.get("egw_token", ""),
                egw_route=row.get("egw_route", ""),
                egw_expected_ip=row.get("egw_expected_ip", ""),
                pinned=bool(row.get("pinned", False)),
                owner_id=row.get("owner_id"),
            )
            self._accounts[account.id] = account

    def _persist(self) -> None:
        rows = [
            {
                "id": a.id, "label": a.label, "index": a.index,
                "profile_dir_name": a.profile_dir_name, "api_key": a.api_key,
                "egw_token": a.egw_token, "egw_route": a.egw_route,
                "egw_expected_ip": a.egw_expected_ip,
                "pinned": a.pinned, "owner_id": a.owner_id,
            }
            for a in self.list()
        ]
        REGISTRY_PATH.write_text(
            json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        # Every row carries a plaintext platform api_key; the default umask
        # would otherwise leave this group/world-readable.
        os.chmod(REGISTRY_PATH, 0o600)

    def list(self) -> list[Account]:
        return sorted(self._accounts.values(), key=lambda a: a.index)

    def get(self, account_id: str) -> Account | None:
        return self._accounts.get(account_id)

    def _generate_account_id(self) -> str:
        # Random, not chosen by hand: shop id is now internal plumbing (a
        # config filename / profile dir / database key), not something an
        # operator needs to type or remember - the display label is what
        # they actually see and pick.
        for _ in range(50):
            candidate = f"shop-{secrets.token_hex(4)}"
            if candidate not in self._accounts:
                return candidate
        raise RuntimeError("could not generate a unique workspace id")

    def create(
        self, account_id: str = "", label: str = "",
        *, profile_dir_name: str = "", api_key: str = "",
        egw_token: str = "", owner_id: int | None = None,
    ) -> Account:
        with self.lock:
            account_id = account_id.strip() or self._generate_account_id()
            _validate_name(account_id, "account_id")
            if profile_dir_name.strip():
                _validate_name(profile_dir_name.strip(), "profile_dir_name")
            if account_id in self._accounts:
                raise ValueError(f"account already exists: {account_id}")
            used = {a.index for a in self._accounts.values()}
            index = next(i for i in range(0, 100) if i not in used)
            account = Account(
                id=account_id, label=label or account_id, index=index,
                profile_dir_name=profile_dir_name.strip(), api_key=api_key.strip(),
                egw_token=egw_token.strip(), owner_id=owner_id,
            )
            self._accounts[account_id] = account
            self.write_config(account)
            self._persist()
            logger.info("[accounts] created %s (index=%s cdp=%s control=%s)",
                        account.id, account.index, account.cdp_port, account.control_port)
            return account

    def update(
        self, account_id: str, *, label: str | None = None, api_key: str | None = None,
        egw_token: str | None = None, egw_route: str | None = None,
        egw_expected_ip: str | None = None,
    ) -> Account | None:
        """Edit an existing account's label, Gemini API key and/or egw token.

        `profile_dir_name` is deliberately not editable here - it is the
        Chrome profile directory on disk; changing it after the workspace
        has already logged in would silently point the account at a
        different (empty) profile instead of the one holding its session.
        `None` means "leave unchanged" for each field; api_key="" is a valid
        way to clear a previously set key back to the shared default.
        """
        account = self._accounts.get(account_id)
        if account is None:
            return None
        if label is not None:
            stripped = label.strip()
            if not stripped:
                raise ValueError("Tên hiển thị không được để trống")
            account.label = stripped
        if api_key is not None:
            account.api_key = api_key.strip()
        if egw_token is not None:
            account.egw_token = egw_token.strip()
        if egw_route is not None:
            account.egw_route = egw_route.strip()
        if egw_expected_ip is not None:
            account.egw_expected_ip = egw_expected_ip.strip()
        self.write_config(account)
        self._persist()
        return account

    def set_pinned(self, account_id: str, pinned: bool) -> Account | None:
        account = self._accounts.get(account_id)
        if account is None:
            return None
        account.pinned = pinned
        self._persist()
        return account

    def delete(self, account_id: str, *, purge: bool = False) -> bool:
        account = self._accounts.pop(account_id, None)
        if account is None:
            return False
        # The config is removed; by default the Chrome profile and the
        # database are left alone because they hold the session and the
        # customer history - `purge` is the explicit opt-in to also wipe the
        # Chrome profile (cookies/session) and this account's data directory
        # (products/media). The database is never touched here either way:
        # conversation/task history stays queryable by account_id even after
        # the workspace itself is gone.
        account.config_path.unlink(missing_ok=True)
        if purge:
            shutil.rmtree(account.profile_dir, ignore_errors=True)
            shutil.rmtree(BASE_DIR / "data" / "accounts" / account.id, ignore_errors=True)
            logger.info("[accounts] purged profile+data for %s", account.id)
        self._persist()
        return True

    # ------------------------------------------------------------ config
    def write_config(self, account: Account) -> Path:
        """Write this account's overlay (see `render_config`) atomically."""
        content = self.render_config(account)
        account.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = account.config_path.with_suffix(".yaml.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            yaml.safe_dump(content, stream, allow_unicode=True, sort_keys=False, indent=2)
        if account.api_key or account.egw_token:
            # This file may carry the same plaintext secrets as the registry.
            os.chmod(temporary, 0o600)
        os.replace(temporary, account.config_path)
        return account.config_path

    def render_config(self, account: Account) -> dict:
        """This account's overlay on the shared base config, header included.

        The file holds only what is this workspace's own: its identity (id,
        ports, paths, keys - rebuilt from `account` on every call, so a revoked
        egw token or API key disappears) plus whatever an operator set for it
        from the dashboard (kept as is). Everything else is read from the base
        config at load time - see load_config.EXTENDS_KEY. A legacy full copy
        is converted on the way (account_config.overlay_from_legacy).
        """
        base = read_yaml(self.base_config_path)
        existing = None
        if account.config_path.exists():
            try:
                existing = read_yaml(account.config_path)
            except (OSError, yaml.YAMLError):
                existing = None
        if existing is None:
            # New workspace: born under today's defaults, so no migration
            # written before now applies to it.
            data, version = {}, CURRENT_CONFIG_VERSION
        elif account_config.is_overlay(existing):
            data, version = account_config.split_header(existing)
        else:
            data, version = account_config.overlay_from_legacy(existing, base), CURRENT_CONFIG_VERSION
        version = account_config.migrate(data, version)
        # Rebuilt below from the account itself; dropped first so that what
        # the account no longer has (an egw token, an API key) goes too.
        data.pop("egw", None)
        if isinstance(data.get("llm"), dict):
            data["llm"].pop("api_key", None)

        overrides = {
            ("account", "id"): account.id,
            ("account", "label"): account.label,
            ("account", "index"): account.index,
            ("control", "enabled"): True,
            ("control", "host"): "127.0.0.1",
            ("control", "port"): account.control_port,
            ("browser", "cdp_url"): f"http://127.0.0.1:{account.cdp_port}",
            # storage.database_url is inherited from the base config on
            # purpose: all accounts share one PostgreSQL database.
            ("paths", "screenshots"): f"data/accounts/{account.id}/screenshots",
            ("paths", "logs"): f"data/accounts/{account.id}/logs",
            ("paths", "temp"): f"data/accounts/{account.id}/temp",
            ("logging", "file_path"): f"data/accounts/{account.id}/logs/agent.log",
            # Fixed coordinates are screen-resolution specific. Two profiles with
            # different window sizes would click the wrong place, silently, so
            # the shared coordinate file is not inherited by extra accounts.
            ("grounding", "fixed_coordinates_enabled"): account.index == 0,
            # Each account manages its own catalogue - the dashboard's product
            # editor writes to this same per-account directory.
            ("products", "product_root"): f"data/accounts/{account.id}/products",
            # No model here: llm.model / vision.model / ai.planner.model come
            # from configs/config.yaml, so one edit there switches every
            # workspace (each at its agent's next start).
        }
        if account.api_key:
            # The messaging LLM reads this as a fallback when GEMINI_API_KEY is
            # unset (main.py `_build_message_runtime`); the vision/planner paths
            # stay env-var-only, covered instead by injecting GEMINI_API_KEY
            # into this account's own agent process (see processes.py).
            overrides[("llm", "api_key")] = account.api_key
        if account.egw_token and egw_feature_enabled():
            # Every egw-enabled Chrome points at the same proxy address; the
            # route (public IP) is chosen by egw from workspace_id/token at
            # CONNECT time. The env var is read here, once, and the agent
            # (main.py) and Chrome spawn (processes.py) both read this YAML,
            # so a later env change cannot leave the two disagreeing.
            overrides[("egw", "workspace_id")] = account.id
            overrides[("egw", "proxy_url")] = os.environ.get(
                "EGW_PROXY_URL", DEFAULT_EGW_PROXY_URL,
            )
            overrides[("egw", "token")] = account.egw_token
            if account.egw_expected_ip:
                overrides[("egw", "expected_ip")] = account.egw_expected_ip
        for path, value in overrides.items():
            account_config.set_path(data, ".".join(path), value)

        header = {
            EXTENDS_KEY: os.path.relpath(
                self.base_config_path.resolve(), account.config_path.parent.resolve(),
            ),
            VERSION_KEY: version,
        }
        return {**header, **data}

    def legacy_configs(self) -> list[Account]:
        """Workspaces whose YAML is still a full copy of an old base config -
        see scripts/migrate_account_configs.py."""
        found = []
        for account in self.list():
            try:
                data = read_yaml(account.config_path)
            except (OSError, yaml.YAMLError):
                continue
            if not account_config.is_overlay(data):
                found.append(account)
        return found

    def migrate_overlays(self) -> list[str]:
        """Bring every overlay up to CURRENT_CONFIG_VERSION. Legacy full copies
        are left for the migration script, which shows what would change."""
        migrated = []
        for account in self.list():
            try:
                data = read_yaml(account.config_path)
            except (OSError, yaml.YAMLError):
                continue
            if account_config.is_overlay(data) and int(
                data.get(VERSION_KEY) or 0
            ) < CURRENT_CONFIG_VERSION:
                self.write_config(account)
                migrated.append(account.id)
        return migrated

    def config(self, account: Account):
        if not account.config_path.exists():
            self.write_config(account)
        return load_config(account.config_path)

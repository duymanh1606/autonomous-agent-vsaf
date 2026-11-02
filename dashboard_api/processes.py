"""Spawn, watch and stop the child processes belonging to one account.

Two rules shape this module:

* An agent must outlive the supervisor. Children are started detached from the
  supervisor's own lifetime, so a dashboard crash never kills a running agent
  mid-conversation.
* Stopping is graceful first. SIGTERM lets the agent finish the task in flight
  and mark it complete; SIGKILL leaves `tasks.status='running'`, which the next
  boot silently requeues.
"""

import asyncio
import json
import logging
import os
import secrets
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from load_config import BASE_DIR, load_config

from dashboard_api.accounts import DEFAULT_EGW_PROXY_URL, egw_feature_enabled


logger = logging.getLogger(__name__)

GRACEFUL_STOP_SECONDS = 20

CHROME_BINARIES = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
)


def find_chrome() -> str | None:
    for name in CHROME_BINARIES:
        found = shutil.which(name)
        if found:
            return found
    return None


# Chrome's window inside its own dedicated Xvfb screen (one workspace, one
# display, one window - see AccountProcesses.start_chrome). Used to be
# deliberately smaller than a screen shared by several workspaces' windows so
# they stayed distinguishable in vnc.html; that reason is gone now that
# nothing else is ever drawn on the same display, but the size still matters
# for TikTok's own responsive layout (see below), so it stays configurable.
#
# Overridable rather than hardcoded because the safe lower bound is a property
# of TikTok's own responsive layout, which only a live page can tell you:
# narrow the viewport far enough and TikTok rearranges the top-nav login
# controls that auth/auto_login's selectors look for. After changing this,
# check the agent log for `mode=panel` on the inbox drawer and for auto_login
# still resolving its targets - those are the two things a width change breaks
# first.
CHROME_WINDOW_SIZE = os.environ.get("AGENT_CHROME_WINDOW_SIZE", "1100,680")

# The dedicated Xvfb screen each workspace's Chrome renders into. No longer
# needs to be bigger than the window to leave room for other workspaces'
# windows (see CHROME_WINDOW_SIZE above) - kept generous by default anyway
# since a bigger virtual screen costs nothing (Xvfb never actually paints
# pixels beyond what's asked for) and some pages assume a realistic desktop
# size when deciding their own layout.
XVFB_RESOLUTION = os.environ.get("AGENT_XVFB_RESOLUTION", "1920x1080x24")

_PACK_EXTENSION_SCRIPT = BASE_DIR / "scripts" / "pack_extension.sh"
_EXTENSION_ID_FILE = BASE_DIR / "extension_build" / "extension_id.txt"
_EXTENSION_VERSION_FILE = BASE_DIR / "extension_build" / ".version"
_pack_extension_lock = asyncio.Lock()


# Everything in a Chrome profile that is keyed by extension id. All of it has
# to go together: leaving any one behind is what produced the failure mode
# documented in `_reset_stale_extension_profile`.
_EXTENSION_PROFILE_DIRS = (
    "Extensions",
    "Local Extension Settings",
    "Extension Rules",
    "Extension State",
)


def _installed_extension_versions(extension_dir: Path) -> list[str]:
    """Version directories Chrome actually has on disk, e.g. ['0.2.1.8_0']."""
    if not extension_dir.is_dir():
        return []
    return sorted(item.name for item in extension_dir.iterdir() if item.is_dir())


def _extension_version_in_preferences(preferences_path: Path, extension_id: str) -> str | None:
    """The version this profile's Preferences claims is installed, if any.

    Returns None when the profile has no record of the extension at all -
    which is the healthy "not installed yet" state that lets the forcelist
    policy install it on the next launch.
    """
    if not preferences_path.is_file():
        return None
    try:
        data = json.loads(preferences_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = ((data.get("extensions") or {}).get("settings") or {}).get(extension_id)
    if not isinstance(entry, dict):
        return None
    version = (entry.get("manifest") or {}).get("version")
    return str(version) if version else ""


def _forget_extension_in_preferences(preferences_path: Path, extension_id: str) -> None:
    """Drop this extension's install record from the profile's Preferences.

    Chrome treats that record - not the files - as the answer to "is this
    installed?". Deleting the files while the record stays behind leaves a
    profile that believes the extension is installed and healthy, so the
    forcelist never reinstalls it and the DOM bridge is simply absent from
    every page forever (confirmed live: a workspace failed every inbox scan
    for 20+ minutes with `[EXT] rejected reason=bridge_unreadable` in exactly
    this state, and only removing this record brought it back).

    Best-effort by design: an unreadable/absent Preferences file means there
    is no record to remove, which is already the state we want.
    """
    if not preferences_path.is_file():
        return
    try:
        data = json.loads(preferences_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    settings = ((data.get("extensions") or {}).get("settings") or {})
    if extension_id not in settings:
        return
    settings.pop(extension_id, None)
    try:
        preferences_path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    except OSError:
        logger.exception("[extension] could not rewrite %s", preferences_path)


def _reset_stale_extension_profile(account) -> None:
    """Make this account's Chrome profile forget any DOM bridge extension
    install that is not the freshly packed version, so the forcelist does a
    clean reinstall at next launch.

    Chrome's own background updater is NOT reliable for this: confirmed
    live that a profile kept running an extension version from before
    several code changes, across many full Chrome process restarts - its
    "last checked" timestamp lives inside the profile itself and survives
    a process restart, so a fresh launch alone does not guarantee a fresh
    check. A profile with no install record and no files is what reliably
    forces Chrome to reinstall on next launch (the same self-healing path
    Chrome already uses if antivirus or a user deletes the folder by hand) -
    ordinary, supported recovery behaviour, not an internals hack.

    Both halves matter, and an earlier version of this function removed only
    the files. That left profiles recorded-but-missing: Chrome answered "yes,
    installed" from Preferences, skipped the reinstall, and no bridge was ever
    injected - an unrecoverable state that no number of restarts cleared. So
    a reset is triggered by EITHER half being wrong (wrong version on disk, or
    a record with nothing behind it) and always clears both.

    Callers must confirm no Chrome for this account is alive - including an
    orphan from an earlier supervisor instance, which `ManagedProcess.running`
    does not know about (see `start_chrome`). Deleting these files under a
    live Chrome achieves nothing and actively harms: the running process keeps
    the old extension in memory, and on exit writes its install record back
    with no files behind it - the very state described above.
    """
    if not _EXTENSION_ID_FILE.exists() or not _EXTENSION_VERSION_FILE.exists():
        return
    extension_id = _EXTENSION_ID_FILE.read_text().strip()
    current_version = _EXTENSION_VERSION_FILE.read_text().strip()
    if not extension_id or not current_version:
        return

    profile = Path(account.profile_dir)
    default_dir = profile / "Default"
    preferences_path = default_dir / "Preferences"
    installed = _installed_extension_versions(default_dir / "Extensions" / extension_id)
    recorded = _extension_version_in_preferences(preferences_path, extension_id)

    wanted = f"{current_version}_0"
    files_are_current = installed == [wanted]
    record_matches_files = (recorded is None) == (not installed)
    if files_are_current and record_matches_files:
        return

    logger.info(
        "[extension] %s resetting bridge extension: on disk %s, Preferences "
        "records %r, want %s - clearing both so Chrome reinstalls fresh",
        account.id, installed or "nothing", recorded, current_version,
    )
    for name in _EXTENSION_PROFILE_DIRS:
        shutil.rmtree(default_dir / name / extension_id, ignore_errors=True)
    _forget_extension_in_preferences(preferences_path, extension_id)


async def _repack_extension_if_needed() -> None:
    """Re-sign the DOM bridge extension's .crx if `extension/` changed since
    the last pack, so editing its source and clicking Start is enough to
    pick up the change - no separate manual `pack_extension.sh` run.

    The script itself is the one that decides whether anything actually
    changed (a content hash, see scripts/pack_extension.sh) and auto-bumps
    the version Chrome needs to see to treat it as an update; this just
    calls it on every Chrome start and no-ops in well under a second when
    nothing changed. Best-effort: a failure here (missing script, no
    `google-chrome --headless` support, ...) is logged, not fatal - Chrome
    still starts and simply keeps whatever extension version it already has.
    """
    if not _PACK_EXTENSION_SCRIPT.exists():
        return
    async with _pack_extension_lock:
        try:
            process = await asyncio.create_subprocess_exec(
                str(_PACK_EXTENSION_SCRIPT),
                cwd=str(BASE_DIR),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            output, _ = await asyncio.wait_for(process.communicate(), timeout=60)
        except (OSError, asyncio.TimeoutError) as exc:
            logger.warning("[extension] auto-repack failed to run: %s", exc)
            return
        if process.returncode != 0:
            logger.warning(
                "[extension] auto-repack exited %s:\n%s",
                process.returncode, output.decode(errors="replace"),
            )
        else:
            logger.info(
                "[extension] %s", output.decode(errors="replace").strip().splitlines()[0]
                if output else "auto-repack: no output",
            )


def _process_cmdline(pid: int) -> str:
    """The full command line of a running process, or "" if it is gone."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", errors="replace")


def detect_agent_runtime(account) -> str | None:
    """Which runtime an already-running agent for this account was started
    with, or None when no agent is running.

    Both runtimes are launched with the same config path but a different
    entry point, and the running process is the only record of which was
    used - the supervisor forgets on restart. Guessing instead would report
    the wrong runtime on the dashboard and, after a Stop, start the account
    on the wrong entry point entirely.
    """
    pid = _find_running_pid([str(account.config_path)])
    if pid is None:
        return None
    # One runtime remains. The extension runtime (`run_agent.py`) was removed:
    # nothing could select it any more - the picker had been hidden behind
    # `SHOW_TECHNICAL_INFO = false` - and two runtimes meant two sets of
    # wiring drifting apart, one of them carrying a copy of the
    # never-log-in gate that had not run in production for months.
    return "langgraph"


def _find_running_pid(command: list[str]) -> int | None:
    """Best-effort: is a process from an earlier (since-restarted) supervisor
    instance already running this exact command?

    Matches on the longest path-like argument (the account's own profile/
    config path, already unique per account) rather than the whole command
    line, since that's stable even if flag order changes across versions.
    `-o` picks the OLDEST match, i.e. the orphan rather than anything new.

    The `--` is load-bearing, not decoration: Chrome's longest path-like
    argument is `--user-data-dir=<profile>`, and without the separator pgrep
    reads that leading `--` as one of its own options, exits 2 with
    "unrecognized option", and this function answers "nothing is running" for
    every Chrome that ever lived. Confirmed live against a real running
    workspace - which is why duplicate Chrome windows kept appearing despite
    the adoption path below, and why the profile reset in `start_chrome` could
    fire at a Chrome that was very much alive.
    """
    candidates = [arg for arg in command if "/" in arg]
    if not candidates:
        return None
    pattern = max(candidates, key=len)
    try:
        result = subprocess.run(
            ["pgrep", "-f", "-o", "--", pattern],
            capture_output=True, text=True, timeout=3,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


class ManagedProcess:
    """One child process: the agent runtime, or its Chrome."""

    def __init__(self, name: str, command: list[str], *, cwd: Path | None = None,
                 log_path: Path | None = None, env: dict[str, str] | None = None):
        self.name = name
        self.command = command
        self.cwd = cwd or BASE_DIR
        self.log_path = log_path
        # None means "inherit the supervisor's own environment" - the
        # asyncio default, unchanged for anything that does not need an
        # override.
        self.env = env
        self.process: asyncio.subprocess.Process | None = None
        # Set instead of self.process when start() finds this exact command
        # already alive as an orphan from a previous supervisor instance -
        # not a real asyncio.subprocess.Process handle (we never spawned it,
        # so there's nothing to await), just enough to report status/pid
        # correctly and to signal it on stop().
        self._adopted_pid: int | None = None

    @property
    def running(self) -> bool:
        if self._adopted_pid is not None:
            return _pid_alive(self._adopted_pid)
        return self.process is not None and self.process.returncode is None

    def adopt_if_running(self) -> int | None:
        """Claim a process from an earlier supervisor instance, if one is alive.

        `start()` already does this, but only when an operator presses Start.
        Until then a supervisor that restarted reports every workspace as
        "Stopped" while its agent is still answering customers - the state
        `deploy/README.md` warns about, and the same blind spot that let the
        profile reset fire at a live Chrome.

        Adopts only. Nothing is ever started here.
        """
        if self.running:
            return self.pid
        pid = _find_running_pid(self.command)
        if pid is not None:
            self._adopted_pid = pid
        return pid

    @property
    def pid(self) -> int | None:
        if self._adopted_pid is not None:
            return self._adopted_pid
        return self.process.pid if self.process else None

    async def start(self) -> None:
        if self.running:
            return
        # self.process is only this ManagedProcess object's OWN handle - after
        # a supervisor restart it is always None even if a real process from
        # before the restart is still alive (a genuine orphan, not tracked by
        # any in-memory object anymore). Without this check, starting again
        # here would launch a second real process alongside the orphan
        # instead of noticing it's already running - confirmed live as the
        # cause of duplicate Chrome windows piling up per workspace.
        existing_pid = _find_running_pid(self.command)
        if existing_pid is not None:
            logger.warning(
                "[proc] %s already running as orphan pid=%s (not started by "
                "this supervisor instance) - adopting it instead of "
                "starting a duplicate",
                self.name, existing_pid,
            )
            self._adopted_pid = existing_pid
            return
        stream = None
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            stream = open(self.log_path, "ab", buffering=0)
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=str(self.cwd),
            env=self.env,
            stdout=stream or asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.STDOUT if stream else asyncio.subprocess.DEVNULL,
            # Its own process group, so the child survives a supervisor exit and
            # can be signalled as a group when we do want it gone.
            start_new_session=True,
        )
        logger.info("[proc] started %s pid=%s", self.name, self.process.pid)

    async def stop(self, timeout: float = GRACEFUL_STOP_SECONDS) -> str:
        if not self.running:
            return "not_running"
        if self._adopted_pid is not None:
            # No asyncio.subprocess.Process handle to wait() on - this
            # process was never spawned by this supervisor instance, only
            # discovered running. Signal it directly and poll for exit.
            pid = self._adopted_pid
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            for _ in range(int(timeout * 5)):
                if not _pid_alive(pid):
                    self._adopted_pid = None
                    logger.info("[proc] stopped %s (adopted) gracefully", self.name)
                    return "stopped"
                await asyncio.sleep(0.2)
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            self._adopted_pid = None
            return "killed"
        assert self.process is not None
        try:
            os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=timeout)
            logger.info("[proc] stopped %s gracefully", self.name)
            return "stopped"
        except asyncio.TimeoutError:
            logger.warning(
                "[proc] %s ignored SIGTERM for %ss; killing", self.name, timeout
            )
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                self.process.kill()
            await self.process.wait()
            return "killed"

    async def wait(self) -> int:
        if self.process is None:
            return -1
        return await self.process.wait()


class AccountProcesses:
    """The Chrome + agent pair for one account."""

    def __init__(self, account, runtime: str = "langgraph"):
        self.account = account
        self.runtime = runtime
        log_dir = BASE_DIR / "data" / "accounts" / account.id / "logs"
        # This workspace's own display stack - one Xvfb, one x11vnc, one
        # websockify, none of it shared with any other workspace. Order here
        # matches start order in start_chrome(): the display has to exist
        # before x11vnc can attach to it, and x11vnc's port has to be up
        # before websockify has anything to bridge to.
        self.xvfb = ManagedProcess(
            f"{account.id}:xvfb", self._xvfb_command(),
            log_path=log_dir / "xvfb.log",
        )
        self.x11vnc = ManagedProcess(
            f"{account.id}:x11vnc", self._x11vnc_command(),
            log_path=log_dir / "x11vnc.log",
        )
        self.novnc = ManagedProcess(
            f"{account.id}:novnc", self._novnc_command(),
            log_path=log_dir / "novnc.log",
        )
        self.chrome = ManagedProcess(
            f"{account.id}:chrome", self._chrome_command(),
            log_path=log_dir / "chrome.log", env=self._chrome_env(),
        )
        self.agent = ManagedProcess(
            f"{account.id}:agent", self._agent_command(runtime),
            log_path=log_dir / "runtime.log", env=self._agent_env(),
        )

    def adopt_running(self) -> dict[str, int]:
        """Claim whichever of this account's processes are already alive.

        Returns a pid per process name found running, empty when the
        workspace really is stopped. Starts nothing.
        """
        adopted = {}
        pairs = (
            ("xvfb", self.xvfb), ("x11vnc", self.x11vnc), ("novnc", self.novnc),
            ("chrome", self.chrome), ("agent", self.agent),
        )
        for name, managed in pairs:
            pid = managed.adopt_if_running()
            if pid is not None:
                adopted[name] = pid
        return adopted

    def _agent_env(self) -> dict[str, str]:
        # A per-account Gemini key overrides the supervisor's own env var for
        # this one child process, so every call site in the agent (vision,
        # planner, messaging) picks it up uniformly - not just the messaging
        # path, which is the only one that also reads it from YAML.
        #
        # A workspace with no key of its own must not silently inherit
        # whatever GEMINI_API_KEY the supervisor happens to run with (e.g. a
        # shared default in .env) - passing `env=None` to the child would do
        # exactly that, since subprocess inherits the parent's environment
        # unchanged.
        #
        # Set to "" rather than deleted, and this is load-bearing: the agent
        # re-reads .env itself at startup (load_config -> load_env_file), and
        # that loader skips a variable only when the name is already present
        # in os.environ. Deleting the key therefore let the shared .env
        # default flow straight back in inside the child - a workspace that
        # was never given a key still answered customers on someone else's
        # credential. An empty value is "present", so the loader leaves it
        # alone, and every reader (`os.environ.get(...)` in main.py's
        # vision/planner/messaging builders) sees a falsy key and refuses.
        env = dict(os.environ)
        env["GEMINI_API_KEY"] = self.account.api_key or ""
        return env

    def _x11_dir(self) -> Path:
        return BASE_DIR / "data" / "accounts" / self.account.id / "x11"

    def _xauth_path(self) -> Path:
        return self._x11_dir() / "xauth"

    def _ensure_xauth(self) -> None:
        """Write this workspace's own X11 authority cookie, if it doesn't
        already have one.

        Xvfb's `-auth <file>` will happily use a file that doesn't exist yet
        for some builds, but relying on that is exactly the kind of "probably
        works" behavior that bit this codebase before (see the profile-reset
        comment in start_chrome below) - so the cookie is generated the same
        way `startx`/`xinit` do it: `xauth add` writes a fresh MIT-MAGIC-COOKIE
        entry without needing a live X server to talk to. This also means
        every workspace's display now actually requires a credential to
        connect to - a real (if secondary) tightening versus the old shared
        display, which had none.

        Idempotent: an existing file is left alone, so a supervisor restart
        never invalidates a display that's still up and already has clients
        (Chrome, x11vnc) holding the old cookie.
        """
        xauth = shutil.which("xauth")
        if xauth is None:
            logger.warning(
                "[proc] %s: xauth not installed - Xvfb will run without "
                "cookie authentication (run deploy/setup_virtual_display.sh)",
                self.account.id,
            )
            return
        path = self._xauth_path()
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600)
        cookie = secrets.token_hex(16)
        subprocess.run(
            [xauth, "-f", str(path), "add", f":{self.account.display_num}", ".", cookie],
            capture_output=True, timeout=5, check=False,
        )

    def _xvfb_command(self) -> list[str]:
        return [
            "Xvfb", f":{self.account.display_num}",
            "-screen", "0", XVFB_RESOLUTION,
            "-nolisten", "tcp",
            # Also the argument _find_running_pid() uses to re-adopt this
            # process after a supervisor restart - it's the one path-like
            # argument in this command, and _find_running_pid() only matches
            # on those (see its own docstring below).
            "-auth", str(self._xauth_path()),
        ]

    def _x11vnc_command(self) -> list[str]:
        return [
            "x11vnc",
            "-display", f":{self.account.display_num}",
            "-auth", str(self._xauth_path()),
            "-forever", "-shared",
            # Loopback-only, no VNC password - unchanged from the old shared
            # instance's posture (see deploy/setup_virtual_display.sh's
            # history). Isolation now comes structurally from one workspace
            # having its own display with nothing else ever drawn on it, not
            # from adding a password here.
            "-localhost",
            "-rfbport", str(self.account.vnc_port),
            "-nopw", "-ncache", "10", "-clear_mods",
            # x11vnc's own "-o": distinguishing path argument for adoption,
            # and a real per-workspace log where the shared instance only had
            # one log for every workspace at once.
            "-o", str(BASE_DIR / "data" / "accounts" / self.account.id / "logs" / "x11vnc.log"),
        ]

    def _novnc_command(self) -> list[str]:
        return [
            "websockify",
            "--web", "/usr/share/novnc",
            "--log-file", str(BASE_DIR / "data" / "accounts" / self.account.id / "logs" / "novnc.log"),
            f"127.0.0.1:{self.account.novnc_port}",
            f"127.0.0.1:{self.account.vnc_port}",
        ]

    def _chrome_env(self) -> dict[str, str]:
        # Every workspace used to inherit the supervisor's own DISPLAY (one
        # value, set once at the systemd level) - which is exactly how every
        # Chrome ended up on the same shared screen. Overriding it per account
        # the same way _agent_env() overrides GEMINI_API_KEY is what actually
        # separates them; XAUTHORITY has to travel with it now that the
        # display requires a cookie (see _ensure_xauth) or Chrome's connection
        # to its own dedicated Xvfb is refused.
        env = dict(os.environ)
        env["DISPLAY"] = f":{self.account.display_num}"
        env["XAUTHORITY"] = str(self._xauth_path())
        return env

    def _chrome_command(self) -> list[str]:
        binary = find_chrome() or "google-chrome"
        # The DOM bridge extension is NOT loaded via --load-extension here -
        # confirmed by direct testing (not assumed) that current Chrome
        # silently refuses to actually run an extension loaded that way,
        # regardless of --disable-extensions-except/--enable-unsafe-extension-
        # debugging/pre-seeded developer_mode. It is instead force-installed
        # into every Chrome profile on this machine via a Chrome enterprise
        # policy (ExtensionInstallForcelist) - see scripts/pack_extension.sh
        # and scripts/install_extension_policy.sh, and the README - a
        # one-time, per-machine setup step, not per-workspace.
        return [
            binary,
            f"--remote-debugging-port={self.account.cdp_port}",
            f"--user-data-dir={self.account.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            # Notifications are deliberately NOT disabled here (no
            # --disable-notifications): TikTok's own message-request queue
            # does not refresh on its own while the page sits loaded, and a
            # real desktop Notification firing may be the only live signal
            # available for a stranger's first message - see
            # platforms/tiktok/inbox_watcher.py's _maybe_reload_for_new_
            # activity and tools/browser.py's ChromeBrowserTool.start(),
            # which pre-grants the permission itself so no permission-prompt
            # banner ever sits on top of the page blocking a click.
            #
            # No window manager runs on this display at all (each workspace
            # has its own dedicated Xvfb - see _xvfb_command - with exactly
            # one window ever drawn on it), unlike the old shared-display
            # setup, which needed fluxbox specifically to keep several
            # workspaces' windows from opening at the exact same forced
            # position and hiding each other with no way to switch. With
            # nothing else on the display, there's nothing to place around.
            #
            # Size and the reasoning behind it: see CHROME_WINDOW_SIZE above.
            f"--window-size={CHROME_WINDOW_SIZE}",
            *self._egw_proxy_flags(),
            "https://www.tiktok.com",
        ]

    def _egw_proxy_flags(self) -> list[str]:
        """`--proxy-server` for egw, only for an account that opted in.

        The switch is the account's egw_token, not any EGW_* environment
        variable: an unconditional flag would aim every Chrome on a machine
        without egw at a port nobody listens on and take every workspace
        offline at once. The address comes from the account's own YAML
        (written by `AccountRegistry.write_config`), the same file the agent
        reads, so Chrome and the CDP auth handler can never disagree about
        which proxy is in use.
        """
        if not egw_feature_enabled() or not getattr(self.account, "egw_token", ""):
            return []
        proxy_url = ""
        if self.account.config_path.exists():
            proxy_url = load_config(self.account.config_path).get("egw.proxy_url", "")
        return [f"--proxy-server={proxy_url or DEFAULT_EGW_PROXY_URL}"]

    def _agent_command(self, runtime: str) -> list[str]:
        # `runtime` is kept in the signature because the API and the stored
        # process info still carry it; there is one entry point now.
        return [
            sys.executable, "main.py", "--config", str(self.account.config_path),
        ]

    # ------------------------------------------------------------ control
    async def start_chrome(self) -> None:
        """Bring up this workspace's own display stack, then Chrome into it.

        Name kept for backward compatibility with callers (sleep_wake.py,
        main.py's start route) that only ever needed "make this workspace's
        browser usable" - they do not need to know the display stack lives
        underneath it now.
        """
        self._ensure_xauth()
        await self.xvfb.start()
        # Flat sleeps, not a socket/port poll, matching this codebase's
        # existing style for "give the previous step a moment" (the Chrome ->
        # agent transition below does the same). Xvfb/x11vnc have no
        # flakiness precedent here to guess against, unlike Chrome's CDP port
        # (see sleep_wake.py's _wait_for_cdp) - revisit with a real poll only
        # if manual testing surfaces an actual race, not preemptively.
        await asyncio.sleep(0.5)
        await self.x11vnc.start()
        await asyncio.sleep(0.5)
        await self.novnc.start()

        await _repack_extension_if_needed()
        # This object is cached per account for the supervisor's lifetime, but
        # the account may have opted in to / out of egw since it was built.
        # Recompute before spawning; orphan adoption is unaffected because
        # `_find_running_pid` matches on the profile path, not the flags.
        if not self.chrome.running:
            self.chrome.command = self._chrome_command()
        # `self.chrome.running` alone is NOT "no Chrome has these files open":
        # it only knows about processes this supervisor instance started, so
        # after a supervisor restart it reads False even while a real orphan
        # Chrome from before the restart is still live (the case
        # `ManagedProcess.start` exists to adopt). Resetting on that reading
        # deleted a running Chrome's extension out from under it, and since
        # `start()` then adopted the orphan instead of launching, nothing ever
        # reinstalled: the workspace kept serving customers on the stale
        # extension, its stored extension state was gone, and the exiting
        # Chrome wrote its install record back with no files behind it -
        # precisely the unrecoverable state `_reset_stale_extension_profile`
        # exists to prevent. Confirmed live end to end, not reasoned about.
        # So ask the same question `start()` will: are we really about to
        # spawn a new process? Only then is the profile ours to rewrite.
        if not self.chrome.running and _find_running_pid(self.chrome.command) is None:
            _reset_stale_extension_profile(self.account)
        await self.chrome.start()

    async def start_agent(self) -> None:
        # `AccountProcesses` itself is cached and reused for this account's
        # whole lifetime (see dashboard_api/main.py's `_procs`), across every
        # Stop/Start an operator does - but `self.agent` was built once, in
        # __init__, with whatever `_agent_env()` returned at that moment
        # frozen onto it. `self.account.api_key` does stay live (the
        # registry mutates the same Account object in place on an edit -
        # see AccountRegistry.update), but that alone does nothing: nobody
        # re-read it into a fresh env dict on a later Start, so a key added,
        # changed, or removed after the very first Start silently kept using
        # whatever GEMINI_API_KEY (present, absent, or a different account's
        # own key) was baked in back then - the "Dừng rồi Chạy lại agent để
        # áp dụng API key mới" hint elsewhere in this codebase was true only
        # for the very first Start. Recomputing here, right before every
        # Start, is what actually makes Stop+Start apply a key change.
        self.agent.env = self._agent_env()
        await self.agent.start()

    async def sleep_chrome(self) -> str:
        """Stop only Chrome, leaving the agent process (and its profile on
        disk) untouched - the RAM-saving half of the sleep/wake lifecycle.
        Unlike stop_all(), the agent is never touched here: it stays alive,
        paused, waiting to be resumed once Chrome is started again.
        """
        return await self.chrome.stop()

    async def stop_all(self, *, include_chrome: bool = True) -> dict:
        # Chrome's login/session lives in its --user-data-dir profile on
        # disk, not in the running process - closing Chrome does not force a
        # fresh manual login next start, so there is no real reason to leave
        # it running once the agent that uses it is stopped. Kept as a
        # parameter (not hardcoded) only so a caller with a specific reason
        # to keep the browser open (e.g. an operator mid-manual-session)
        # still can.
        #
        # xvfb/x11vnc/novnc exist only to give Chrome somewhere to render and
        # somewhere to be watched - there is no scenario for keeping them up
        # once Chrome itself is being stopped (unlike sleep_chrome(), which
        # deliberately leaves them running so an open noVNC tab does not drop
        # mid-sleep). Stopped in reverse of start order: consumers before the
        # display they depend on.
        result = {"agent": await self.agent.stop()}
        if include_chrome:
            result["chrome"] = await self.chrome.stop()
            result["novnc"] = await self.novnc.stop()
            result["x11vnc"] = await self.x11vnc.stop()
            result["xvfb"] = await self.xvfb.stop()
        return result

    def snapshot(self) -> dict:
        return {
            "chrome": {"running": self.chrome.running, "pid": self.chrome.pid},
            "agent": {"running": self.agent.running, "pid": self.agent.pid,
                      "runtime": self.runtime},
        }

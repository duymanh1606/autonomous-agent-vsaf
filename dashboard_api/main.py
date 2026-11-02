"""The dashboard supervisor API.

    python -m dashboard_api               # http://127.0.0.1:28080 (SUPERVISOR_PORT)

Owns nothing the agent owns: it starts and stops processes, reads their
databases, proxies control calls, and merges their event streams. Every decision
about what the agent should do stays inside the agent.

Loopback only, no authentication - the same posture as the extension bridge.
"""

if __name__ == "__main__":
    # Refused before any state is built: run as a script this module would be
    # `__main__`, and the routes in `routes/` would import a second copy with
    # its own account registry (see dashboard_api/__main__.py).
    raise SystemExit("run the supervisor with: python -m dashboard_api")

import asyncio
import contextlib
import json
import logging
import math
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import uvicorn
from fastapi import (
    Body, FastAPI, Header, HTTPException, Query,
    Request, Response, WebSocket, WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from dashboard_api import auth, cf_access, security_config, egw, logs, mailer
from dashboard_api.accounts import AccountRegistry, InvalidAccountName
from control import auth as control_auth
from dashboard_api.control_writer import ConversationControlWriter
from dashboard_api.routes import chat_media
from dashboard_api.routes import orders as order_routes
from dashboard_api.routes import push as push_routes
from dashboard_api.routes import products as product_routes
from dashboard_api.routes import translations as translation_routes
from dashboard_api.db_reader import AgentDatabaseReader
from dashboard_api.hub import EventHub
from dashboard_api.notifications import NotificationStore
from dashboard_api.orchestrator import MAX_RUNNING_PLANS_PER_OWNER, Orchestrator
from dashboard_api.orders import OrderStore
from dashboard_api.processes import AccountProcesses, detect_agent_runtime, find_chrome
from core.schedule import OperatingHours
from storage.database import prepare_database
from dashboard_api.alerts import AlertDispatcher, EscalationMailer
from dashboard_api.health_monitor import HealthMonitor
from dashboard_api.search import fold, matches_words, snippet
from dashboard_api.sleep_wake import SleepWakeManager
from dashboard_api.translations import TranslationStore
from dashboard_api.web_push import PushStore, WebPushNotifier
from dashboard_api.users import UserStore
from integrations.sheet_export import close_client as close_sheet_client
from agent.followup_policy import (
    MAX_DELAY_MINUTES as FOLLOWUP_MAX_DELAY_MINUTES,
    MAX_MESSAGE_CHARACTERS as FOLLOWUP_MAX_MESSAGE_CHARACTERS,
    MAX_RULES as FOLLOWUP_MAX_RULES,
    MIN_DELAY_MINUTES as FOLLOWUP_MIN_DELAY_MINUTES,
)
from agent.reply_generator import (
    MAX_STYLE_RULE_CHARACTERS as REPLY_STYLE_MAX_RULE_CHARACTERS,
    MAX_STYLE_RULES as REPLY_STYLE_MAX_RULES,
)
from agent.reply_language import (
    DEFAULT_REPLY_LANGUAGE,
    default_reply_style,
    REPLY_LANGUAGES,
    language_label,
)
from agent.shop_policy import clean_shipping_setting
from agent.scenario_policy import (
    MAX_EXAMPLES as SCENARIO_MAX_EXAMPLES,
    MAX_FIELD_CHARACTERS as SCENARIO_MAX_FIELD_CHARACTERS,
)
from storage.database import resolve_dsn
from storage.llm_usage import LlmUsageStore
from storage.paths import get_account_media_dir, get_account_upload_dir
from storage.product_repository import ProductRepository
from load_config import BASE_DIR, load_config


logger = logging.getLogger(__name__)

# See the /api/events handler and main() for why both of these exist.
EVENTS_HEARTBEAT_SECONDS = 5
HEARTBEAT_FRAME = '{"type":"heartbeat"}'
GRACEFUL_SHUTDOWN_SECONDS = 10

CONTROL_TIMEOUT = 10.0

# A control call that takes longer than this is worth a line in the log on its
# own. The agent embeds its control server in its own event loop, so a slow
# call means the agent was busy in a stretch that does not yield - not that
# the control work itself is expensive (the handlers log their own
# `elapsed_ms`, which is database time only). Every operator-visible action
# goes through here, so this is the one place that sees the whole wait.
CONTROL_SLOW_CALL_MS = 250.0

registry = AccountRegistry()
hub = EventHub()
notifications = NotificationStore()
orders_store = OrderStore()
translation_store = TranslationStore()
push_store = PushStore()
users_store = UserStore()
usage_store = LlmUsageStore()
processes: dict[str, AccountProcesses] = {}
readers: dict[str, AgentDatabaseReader] = {}
# Only reached when a workspace's own control server does not answer - see
# dashboard_api/control_writer.py for why that fallback has to exist at all.
control_writers: dict[str, ConversationControlWriter] = {}
product_repos: dict[str, ProductRepository] = {}
# Pushes to browsers with the dashboard closed (dashboard_api/web_push.py).
# The label is looked up late: _account_label is defined further down.
web_push = WebPushNotifier(push_store, label_for=lambda account_id: _account_label(account_id))


def _persist_notification(event: dict) -> None:
    try:
        notifications.from_event(event)
    except Exception as exc:
        # A feed that cannot be written must not break the live stream.
        logger.warning('[notify] %s', type(exc).__name__, exc_info=True)
    try:
        web_push.on_event(event)
    except Exception:
        logger.exception('[push] queueing a push for an event failed')


hub.on_event = _persist_notification

@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    # Schema, migrations and row-level security (storage/tenancy.py) before
    # any scoped reader opens: those connections drop to the tenant role and
    # fail closed if it does not exist yet. A database that is not up yet is
    # logged, not fatal - the dashboard still has to start to say so.
    try:
        await asyncio.to_thread(prepare_database)
    except Exception:
        logger.exception("[db] preparing the database at boot failed")
    # Workspace configs are overlays on configs/config.yaml with versioned
    # migrations (dashboard_api/account_config.py); bring them current before
    # any agent is started from one. A legacy full copy is only reported: its
    # conversion changes behaviour and is reviewed through the script.
    for account_id in registry.migrate_overlays():
        logger.warning("[accounts] %s: config migrated to the current version", account_id)
    legacy = registry.legacy_configs()
    if legacy:
        logger.warning(
            "[accounts] %s workspace(s) still on a full config copy that no longer "
            "follows configs/config.yaml: %s - review and convert with "
            "`uv run python scripts/migrate_account_configs.py`",
            len(legacy), ", ".join(account.id for account in legacy),
        )
    # Following an agent needs a running loop, so it cannot happen at import
    # time. Every known account is followed straight away, which is what makes
    # an agent already running before the dashboard started show up as live.
    for account in registry.list():
        hub.follow(account.id, account.control_port)
    # Before anything reads `processes`: sleep/wake and the dashboard both
    # ask it what is running, and an empty table would tell them "nothing".
    adopt_running_processes()
    sleep_wake.start()
    health_monitor.start()
    yield
    await health_monitor.stop()
    # Agent processes deliberately survive: they run in their own process group
    # so a dashboard restart never interrupts a conversation in flight.
    await sleep_wake.stop()
    await hub.close()
    for reader in readers.values():
        with contextlib.suppress(Exception):
            reader.close()
    with contextlib.suppress(Exception):
        notifications.close()
    with contextlib.suppress(Exception):
        orders_store.close()
    with contextlib.suppress(Exception):
        translation_store.close()
    with contextlib.suppress(Exception):
        users_store.close()
    with contextlib.suppress(Exception):
        usage_store.close()
    with contextlib.suppress(Exception):
        push_store.close()
    with contextlib.suppress(Exception):
        await close_sheet_client()


# Read once, because FastAPI freezes these on the app object; the module-level
# names are what tests assert against.
_DOCS_URL, _REDOC_URL, _OPENAPI_URL = security_config.docs_urls()

app = FastAPI(
    title="Autonomous Agent Dashboard", version="0.1.0", lifespan=lifespan,
    # None in production: these paths sit outside /api/, so neither the auth
    # middleware nor the container nginx covers them, and they hand out the
    # complete route schema to anyone who reaches the supervisor port.
    docs_url=_DOCS_URL, redoc_url=_REDOC_URL, openapi_url=_OPENAPI_URL,
)
# Middleware registration order is the reverse of execution order: whatever is
# added last ends up outermost. Execution must be
#   CORS -> Cloudflare Access -> CSRF/Origin -> session
# so they are registered bottom-up here.
#
#  * session innermost: it does a database read per request, and nothing
#    should reach it that an outer layer was going to reject anyway;
#  * CORS outermost, unchanged from before: its OPTIONS preflight handling has
#    to answer before auth sees the request, and its headers must survive onto
#    a 401 body or the dev SPA shows a network error instead of a login form.
auth.install_middleware(app, users_store)
auth.install_csrf_middleware(app)
cf_access.install_middleware(app)
app.add_middleware(
    CORSMiddleware,
    allow_origins=security_config.cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------ helpers
def _account_or_404(account_id: str):
    account = registry.get(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"unknown account: {account_id}")
    auth.check_account_access(account_id)
    return account


def _owned_account_or_404(account_id: str):
    """Like `_account_or_404`, but only the workspace's creator or an admin
    may pass - being merely granted read/operate access (see
    `user_account_access`) is not enough to edit or delete it."""
    account = registry.get(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"unknown account: {account_id}")
    user = auth.require_current_user()
    if not user["is_admin"] and account.owner_id != user["id"]:
        raise HTTPException(status_code=403, detail="chỉ chủ sở hữu hoặc admin mới được thao tác")
    return account


def _reader(account) -> AgentDatabaseReader:
    reader = readers.get(account.id)
    if reader is None:
        reader = AgentDatabaseReader(account_id=account.id)
        readers[account.id] = reader
    return reader


def _control_writer(account) -> ConversationControlWriter:
    writer = control_writers.get(account.id)
    if writer is None:
        config = registry.config(account)
        writer = ConversationControlWriter(
            account_id=account.id,
            human_takeover_ttl_minutes=config.get(
                "conversation_control.human_takeover_ttl_minutes", 60
            ),
            waiting_human_ttl_minutes=config.get(
                "conversation_control.waiting_human_ttl_minutes", 20
            ),
        )
        control_writers[account.id] = writer
    return writer


async def _control_or_write(account, path: str, payload: dict | None, write):
    """Ask the agent first; write the row ourselves only if it cannot answer.

    Returns `(row, wrote_directly)`.

    `write` is the direct-database equivalent of the call. The agent stays the
    primary path so it keeps publishing its own events, and the supervisor only
    steps in for the case the agent cannot be asked at all - a stopped or
    crashed workspace, where a customer waiting for a person would otherwise be
    unreachable. Any other failure (501 from a runtime without conversation
    control, a 4xx from the handler) is reported as-is: it is an answer.
    """
    try:
        return await _control(account, "POST", path, payload), False
    except HTTPException as exc:
        if exc.status_code != 503:
            raise
        return await write(_control_writer(account)), True


def _products(account) -> ProductRepository:
    repo = product_repos.get(account.id)
    if repo is None:
        # Matches the per-account root `write_config` sets under
        # `products.product_root` - the dashboard and this account's own
        # agent process must always agree on the same catalogue directory.
        repo = ProductRepository(f"data/accounts/{account.id}/products")
        product_repos[account.id] = repo
    return repo


def adopt_running_processes() -> dict[str, dict[str, int]]:
    """Reconcile this supervisor's empty process table with reality at boot.

    Agents are started detached on purpose, so they outlive the supervisor -
    but `processes` starts empty on every boot, so until now a restart made
    every still-running workspace read as "Stopped" on the dashboard, and
    `ManagedProcess.start` only noticed the truth when someone pressed Start.

    Adopts only what is already alive; never starts anything. An account with
    nothing running is deliberately left out of `processes` entirely, so this
    sweep cannot change how a genuinely stopped workspace reports.
    """
    adopted: dict[str, dict[str, int]] = {}
    for account in registry.list():
        if account.id in processes:
            continue
        try:
            managed = AccountProcesses(account, detect_agent_runtime(account) or "langgraph")
            found = managed.adopt_running()
        except Exception:
            # One unreadable account must not stop the others being found.
            logger.exception("[proc] adopting %s failed", account.id)
            continue
        if not found:
            continue
        processes[account.id] = managed
        adopted[account.id] = found
        logger.warning(
            "[proc] adopted %s from before this supervisor started: %s",
            account.id, ", ".join(f"{k}={v}" for k, v in sorted(found.items())),
        )
    return adopted


def _procs(account, runtime: str = "langgraph") -> AccountProcesses:
    managed = processes.get(account.id)
    if managed is None:
        managed = AccountProcesses(account, runtime)
        processes[account.id] = managed
    return managed


async def _control(account, method: str, path: str, payload: dict | None = None):
    url = f"http://127.0.0.1:{account.control_port}{path}"
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=CONTROL_TIMEOUT) as client:
            response = await client.request(
                method, url, json=payload,
                headers=control_auth.headers_for(account.control_port),
            )
            response.raise_for_status()
            elapsed_ms = (time.monotonic() - started) * 1000
            if elapsed_ms >= CONTROL_SLOW_CALL_MS:
                logger.info(
                    "[control] slow call account=%s %s %s elapsed_ms=%.1f",
                    account.id, method, path, elapsed_ms,
                )
            else:
                logger.debug(
                    "[control] account=%s %s %s elapsed_ms=%.1f",
                    account.id, method, path, elapsed_ms,
                )
            return response.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            # The agent refused OUR token (its file is missing or belongs to
            # another agent on this port). Passed through as 401 it would read
            # to the browser as "your dashboard session expired" and log the
            # user out, so it is reported as the gateway problem it is.
            logger.warning(
                "[control] agent rejected the control token account=%s port=%s",
                account.id, account.control_port,
            )
            raise HTTPException(
                status_code=502,
                detail=(
                    "agent rejected the supervisor's control token - "
                    "stop and start this workspace"
                ),
            ) from exc
        raise HTTPException(status_code=exc.response.status_code,
                            detail=exc.response.text) from exc
    except httpx.HTTPError as exc:
        # Not reachable is a normal state (agent stopped), not a server error.
        # The elapsed time separates the two shapes of failure: refused in a
        # millisecond (the workspace is stopped) versus timed out after
        # CONTROL_TIMEOUT (the agent is running but its loop never got to us).
        logger.info(
            "[control] call failed account=%s %s %s elapsed_ms=%.1f error_type=%s",
            account.id, method, path,
            (time.monotonic() - started) * 1000, type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail=f"agent not reachable on port {account.control_port}",
        ) from exc


# Shared base config (configs/config.yaml) read once for the `sleep:` section
# - this is supervisor-only behaviour, not one of AccountRegistry.write_config's
# per-account overrides, so every workspace uses the same sleep/wake settings.
sleep_config = load_config()
sleep_wake = SleepWakeManager(
    registry=registry, processes=processes, hub=hub,
    control=_control, config=sleep_config,
)

# Read-only checks over rows the agent already writes, mirrored into the same
# notifications the dashboard shows. Nothing here instruments the agent, so a
# monitoring fault cannot slow or break a customer reply - see
# dashboard_api/health_monitor.py.
def _alert_worthy(row: dict) -> bool:
    """Hold back "agent not reachable" for a workspace nobody expects to run.

    The hub follows every account at boot, so a workspace that was simply
    stopped also has that row open. Expected to run = started or adopted by
    this supervisor (`processes`) and not stopped since (still followed); a
    crashed agent is both, which is exactly the one worth a mail.
    """
    if row.get("kind") == "health":
        account_id = row.get("account_id")
        return account_id in processes and hub.is_following(account_id)
    return True


def _account_label(account_id: str) -> str:
    account = registry.get(account_id)
    return account.label if account is not None else account_id


def _expected_to_answer(account_id: str) -> bool:
    """False while nobody expects this workspace to answer: stopped by an
    operator (unfollowed, or never started by this supervisor), or paused -
    by an operator or by auto-sleep. An agent that should run but does not
    answer counts as answering here: the "agent not reachable" alert is the
    one for that, and customers waiting on it must not be hidden.
    """
    account = registry.get(account_id)
    if account is None or account_id not in processes or not hub.is_following(account_id):
        return False
    try:
        response = httpx.get(
            f"http://127.0.0.1:{account.control_port}/control/status",
            headers=control_auth.headers_for(account.control_port), timeout=3,
        )
        response.raise_for_status()
        return not response.json().get("paused")
    except (httpx.HTTPError, ValueError):  # unreachable: see the docstring
        return True


def _within_operating_hours(account_id: str) -> bool:
    account = registry.get(account_id)
    if account is None:
        return False
    return OperatingHours.from_config(registry.config(account)).is_open()


# Mails what the monitor and the hub open, so a fault reaches someone who is
# not looking at the dashboard; see dashboard_api/alerts.py (ALERT_EMAILS,
# ALERT_HEARTBEAT_URL).
health_monitor = HealthMonitor(
    notifications=notifications,
    database_url_resolver=resolve_dsn,
    list_accounts=registry.list,
    alerts=AlertDispatcher(
        notifications, should_alert=_alert_worthy, label_for=_account_label,
    ),
    is_open=_within_operating_hours,
    is_answering=_expected_to_answer,
    escalation_mailer=EscalationMailer(
        notifications, recipients_for=users_store.workspace_emails,
        label_for=_account_label,
    ),
)


# ------------------------------------------------------------------- auth
@app.post("/api/auth/register")
def register(
    request: Request,
    email: str = Body(..., embed=True), password: str = Body(..., embed=True),
) -> dict[str, Any]:
    # Throttles scripted mass account creation - checked *and consumed*
    # before anything else, keyed by source IP. The budget is spent up front
    # rather than only on the success path: this endpoint reports a taken
    # address explicitly (see below), so a free "already registered" response
    # would make enumerating accounts unlimited instead of merely possible.
    ip = auth.client_ip(request)
    auth.register_limiter.check(ip)
    auth.register_limiter.hit(ip)
    email = auth.normalize_email(email)
    # Shape errors are safe to report precisely: they say nothing about who
    # already has an account.
    if error := auth.gmail_error(email):
        raise HTTPException(status_code=400, detail=error)
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="mật khẩu cần tối thiểu 8 ký tự")
    # A taken address is reported plainly, as an operator decision: this is
    # an internal dashboard for one shop's staff, where someone retyping an
    # address they already registered needs to be told so - being answered
    # "waiting for an admin" and then never approved is the worse failure.
    #
    # The cost is real and accepted: together with the rate limit above, this
    # lets someone test roughly 20 addresses per 5 minutes per IP for "does
    # this address have an account here?". `forgot-password` below keeps the
    # opposite discipline and stays deliberately silent, because there the
    # answer would also hint at *which* addresses are worth attacking.
    if users_store.get_by_email(email) is not None:
        raise HTTPException(status_code=409, detail="email đã được đăng ký")
    # Self-registration only ever produces a "user" account - an admin
    # account can only come from `python -m dashboard_api.create_admin`,
    # run by hand against the backend - and it lands inactive: a stranger
    # who signs up on a public URL must not be able to create workspaces
    # (each of which spawns a Chrome and an agent process on this host)
    # before a human has looked at them.
    users_store.create_user(
        email, auth.hash_password(password), is_admin=False, is_active=False,
    )
    logger.info("[auth] registration pending approval for %s", email)
    # No session cookie: an unapproved account has nothing to be logged in for.
    return {"ok": True, "pending_approval": True}


@app.post("/api/auth/login")
def login(
    request: Request,
    response: Response,
    email: str = Body(..., embed=True), password: str = Body(..., embed=True),
) -> dict[str, Any]:
    email = auth.normalize_email(email)
    if auth.email_error(email):
        raise HTTPException(status_code=400, detail="email không hợp lệ")
    # Three axes, because each one alone leaves a hole:
    #  * email - keyed this way so a shared office connection never locks out
    #    every real operator over one person's mistyped password;
    #  * IP - without it, one source can try a single common password against
    #    thousands of addresses and never trip the email counter;
    #  * global - a spray distributed across many source IPs trips neither.
    ip = auth.client_ip(request)
    auth.login_limiter.check(email)
    auth.login_ip_limiter.check(ip)
    auth.login_global_limiter.check(auth.LOGIN_GLOBAL_KEY)
    row = users_store.get_by_email(email)
    used_temp_password = False
    if row is not None and not auth.verify_password(password, row["password_hash"]):
        # The mailed temporary password, while it lasts. Checked second and
        # counted by the same limiters, so it is no extra way in.
        expires = row.get("temp_password_expires_at")
        used_temp_password = bool(
            row.get("temp_password_hash")
            and expires is not None
            and expires > datetime.now(timezone.utc)
            and auth.verify_password(password, row["temp_password_hash"])
        )
        if not used_temp_password:
            row = None
    if row is None:
        auth.login_limiter.hit(email)
        auth.login_ip_limiter.hit(ip)
        auth.login_global_limiter.hit(auth.LOGIN_GLOBAL_KEY)
        raise HTTPException(status_code=401, detail="sai email hoặc mật khẩu")
    if not row["is_active"]:
        raise HTTPException(
            status_code=403,
            detail="tài khoản đang chờ quản trị viên phê duyệt hoặc đã bị vô hiệu hoá",
        )
    if used_temp_password:
        # Swap it in and demand a new one; other sessions die, as with any
        # password change. Watermark first, cookie after (auth.session_revoked).
        users_store.use_temp_password(row["id"])
        users_store.invalidate_sessions(row["id"])
        row = users_store.get_by_id(row["id"])
    # Only the email counter is cleared: a correct password for one account
    # says nothing about the source, so the IP and global budgets stand.
    auth.login_limiter.clear(email)
    auth.set_session_cookie(response, row["id"], request)
    return auth.public_user(row, users_store.accessible_account_ids(row["id"]))


@app.post("/api/auth/logout")
def logout(response: Response) -> dict[str, Any]:
    auth.clear_session_cookie(response)
    return {"ok": True}


@app.get("/api/auth/me")
def read_me() -> dict[str, Any]:
    return auth.require_current_user()


def _mail_temp_password(row: dict[str, Any]) -> None:
    """Mail `row` a temporary password. Its real password keeps working until
    the temporary one is used (see UserStore.use_temp_password)."""
    temp = auth.new_temp_password()
    users_store.set_temp_password(
        row["id"], auth.hash_password(temp),
        datetime.now(timezone.utc) + timedelta(seconds=auth.TEMP_PASSWORD_TTL_SECONDS),
    )
    minutes = auth.TEMP_PASSWORD_TTL_SECONDS // 60
    mailer.send(
        row["email"],
        "Mật khẩu tạm thời",
        f"Mật khẩu tạm thời của bạn là: {temp}\n\n"
        f"Dùng mật khẩu này để đăng nhập trong vòng {minutes} phút; sau khi đăng "
        "nhập bạn sẽ được yêu cầu đặt mật khẩu mới.\n\n"
        "Nếu bạn không yêu cầu, hãy bỏ qua email này - mật khẩu hiện tại của "
        "bạn vẫn dùng được như cũ.",
    )


@app.post("/api/auth/forgot-password")
def forgot_password(request: Request, email: str = Body(..., embed=True)) -> dict[str, Any]:
    auth.forgot_password_limiter.check(auth.client_ip(request))
    auth.forgot_password_limiter.hit(auth.client_ip(request))
    email = auth.normalize_email(email)
    if error := auth.email_error(email):
        raise HTTPException(status_code=400, detail=error)
    row = users_store.get_by_email(email)
    # Everything below answers the same way whatever happens - unknown
    # address, inactive account, per-address limit reached, mail failure - so
    # this endpoint cannot be used to learn which addresses have accounts.
    # The reason is logged, never the address: the page cannot say which
    # addresses have accounts, but an operator asking "why did no mail come"
    # needs an answer that is not "check everything".
    if row is None:
        logger.info("[auth] temporary password not sent: no account for this address")
    elif not row["is_active"]:
        logger.info("[auth] temporary password not sent: account is deactivated")
    else:
        try:
            auth.temp_password_email_limiter.check(email)
            auth.temp_password_email_limiter.hit(email)
            _mail_temp_password(row)
            logger.info("[auth] temporary password mailed user_id=%s", row["id"])
        except HTTPException:
            logger.info("[auth] temporary password not sent: per-address limit (3 per hour)")
        except (mailer.MailNotConfigured, mailer.MailDeliveryFailed) as exc:
            logger.warning(
                "[auth] temporary password not sent error_type=%s detail=%s",
                type(exc).__name__, exc,
            )
    return {"ok": True}


@app.post("/api/auth/reset-password")
def reset_password(token: str = Body(..., embed=True), password: str = Body(..., embed=True)):
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="mật khẩu cần tối thiểu 8 ký tự")
    user_id = users_store.consume_reset_token(token)
    if user_id is None:
        raise HTTPException(status_code=400, detail="link đặt lại mật khẩu không hợp lệ hoặc đã hết hạn")
    users_store.update_password(user_id, auth.hash_password(password))
    # Whoever had a session cookie for this account loses it now. That is the
    # whole point of resetting a password you think someone else has seen -
    # before this, the old cookie kept working forever.
    users_store.invalidate_sessions(user_id)
    return {"ok": True}


@app.post("/api/auth/change-password")
def change_password(
    request: Request,
    response: Response,
    current_password: str = Body(..., embed=True),
    new_password: str = Body(..., embed=True),
) -> dict[str, Any]:
    """A signed-in user changing their own password.

    Asks for the current password even though the caller has a session: a
    session is a cookie on a machine, and someone else sitting at that machine
    must not be able to lock the owner out of their own account.
    """
    user = auth.require_current_user()
    user_id = int(user["id"])
    limiter_key = f"user:{user_id}"
    auth.change_password_limiter.check(limiter_key)
    row = users_store.get_by_id(user_id)
    if row is None or not auth.verify_password(current_password, row["password_hash"]):
        auth.change_password_limiter.hit(limiter_key)
        # 400, not 401: the dashboard reads any 401 as "your session is gone"
        # and signs the user out, which is not what a typo here should do.
        raise HTTPException(status_code=400, detail="mật khẩu hiện tại không đúng")
    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="mật khẩu mới cần tối thiểu 8 ký tự")
    if new_password == current_password:
        raise HTTPException(
            status_code=400, detail="mật khẩu mới phải khác mật khẩu hiện tại",
        )
    users_store.update_password(user_id, auth.hash_password(new_password))
    auth.change_password_limiter.clear(limiter_key)
    # Same as a reset: every other device signed in as this user is signed
    # out, since changing a password is what you do when you think someone
    # else knows it. This browser keeps working - the watermark is bumped
    # *first* and the replacement cookie minted *after*, so its `iat` is past
    # the watermark by construction (see auth.session_revoked).
    users_store.invalidate_sessions(user_id)
    auth.set_session_cookie(response, user_id, request)
    logger.info("[auth] password changed by user_id=%s", user_id)
    return {"ok": True}


# --------------------------------------------------------- user management
_ZERO_USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}


def _owned_workspace_count(user_id: int) -> int:
    """Workspaces this user created - the thing max_workspaces caps.

    Ownership, not access: a workspace an admin merely *granted* to someone
    is not theirs to be counted against them (they did not create it and
    cannot delete it - see _owned_account_or_404). So an admin can hand a
    member of staff ten shops to answer and still keep their quota at 0."""
    return sum(1 for account in registry.list() if account.owner_id == user_id)


def workspace_limit(user: dict[str, Any]) -> int:
    """How many workspaces this user may create. The one place NULL is
    turned into a number, and it turns into 0, not "unlimited".

    That direction matters. An earlier build read NULL as no limit, so every
    row nobody had thought about - including a stranger's fresh signup - could
    create workspaces, and each one spawns a Chrome and an agent process on
    this host. Fail-closed is the house rule: an admin grants a quota
    explicitly or the answer is no."""
    value = user.get("max_workspaces")
    return max(0, int(value)) if value is not None else 0


@app.post("/api/users")
def create_user_as_admin(
    email: str = Body(..., embed=True), password: str = Body(..., embed=True),
) -> dict[str, Any]:
    """Admin-initiated account creation - distinct from the public, rate-
    limited /api/auth/register: no IP throttle (the caller is already an
    authenticated admin), and always `is_admin=False`. Admin accounts are
    deliberately kept to one path only - `python -m dashboard_api.create_admin`
    run by hand on the server - so this can never mint another one, matching
    the invariant documented on UserAccessAdmin in the frontend."""
    auth.require_admin()
    email = auth.normalize_email(email)
    if error := auth.gmail_error(email):
        raise HTTPException(status_code=400, detail=error)
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="mật khẩu cần tối thiểu 8 ký tự")
    if users_store.get_by_email(email) is not None:
        raise HTTPException(status_code=409, detail="email đã được đăng ký")
    row = users_store.create_user(email, auth.hash_password(password), is_admin=False)
    return {
        **_public_managed_user(row),
        "usage": dict(_ZERO_USAGE), "owned_workspaces": 0,
    }


def _public_managed_user(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"], "email": row["email"], "is_admin": row["is_admin"],
        "is_active": row["is_active"], "created_at": row["created_at"],
        "account_ids": [], "max_workspaces": row.get("max_workspaces"),
    }


@app.get("/api/users")
def list_users() -> list[dict[str, Any]]:
    auth.require_admin()
    all_account_ids = [account.id for account in registry.list()]
    result = []
    for user in users_store.list_users():
        relevant = all_account_ids if user["is_admin"] else user["account_ids"]
        by_account = usage_store.totals_by_account(relevant)
        usage = dict(_ZERO_USAGE)
        for totals in by_account.values():
            for key in usage:
                usage[key] += totals[key]
        result.append({
            **user, "usage": usage,
            "owned_workspaces": _owned_workspace_count(user["id"]),
        })
    return result


@app.patch("/api/users/{user_id}/max-workspaces")
def set_user_max_workspaces(
    user_id: int, max_workspaces: int | None = Body(None, embed=True),
) -> dict[str, Any]:
    """How many workspaces this user may create for themselves. null puts
    them back to "chưa cấp", which reads as 0; 0 says the same thing out
    loud. Lowering it below what the user already owns is allowed on
    purpose: it stops the next one but never deletes anything.

    There is no "unlimited" and that is deliberate - every workspace costs a
    Chrome and an agent process on this host, so the cap is a real resource
    budget. An admin who wants to hand out a lot types a big number."""
    auth.require_admin()
    if users_store.get_by_id(user_id) is None:
        raise HTTPException(status_code=404, detail="unknown user")
    if max_workspaces is not None and max_workspaces < 0:
        raise HTTPException(status_code=400, detail="giới hạn workspace không được âm")
    users_store.set_max_workspaces(user_id, max_workspaces)
    return {"max_workspaces": max_workspaces}


@app.post("/api/users/{user_id}/access")
def grant_user_access(user_id: int, account_id: str = Body(..., embed=True)) -> dict[str, Any]:
    auth.require_admin()
    if users_store.get_by_id(user_id) is None:
        raise HTTPException(status_code=404, detail="unknown user")
    _account_or_404_admin(account_id)
    users_store.grant_access(user_id, account_id)
    return {"granted": True}


@app.delete("/api/users/{user_id}/access/{account_id}")
def revoke_user_access(user_id: int, account_id: str) -> dict[str, Any]:
    auth.require_admin()
    users_store.revoke_access(user_id, account_id)
    return {"revoked": True}


def _not_self_or_400(user_id: int) -> None:
    admin = auth.require_admin()
    if admin["id"] == user_id:
        raise HTTPException(status_code=400, detail="không thể tự thao tác lên tài khoản của chính mình")


@app.post("/api/users/{user_id}/deactivate")
def deactivate_user(user_id: int) -> dict[str, Any]:
    _not_self_or_400(user_id)
    if users_store.get_by_id(user_id) is None:
        raise HTTPException(status_code=404, detail="unknown user")
    users_store.set_active(user_id, False)
    # The auth middleware already refuses an inactive user on their next
    # request, so this is belt and braces - but it means the invariant still
    # holds if the account is ever reactivated: sessions from before the
    # deactivation do not come back to life with it.
    users_store.invalidate_sessions(user_id)
    return {"ok": True}


@app.post("/api/users/{user_id}/activate")
def activate_user(user_id: int) -> dict[str, Any]:
    auth.require_admin()
    if users_store.get_by_id(user_id) is None:
        raise HTTPException(status_code=404, detail="unknown user")
    users_store.set_active(user_id, True)
    return {"ok": True}


@app.delete("/api/users/{user_id}")
def delete_user(user_id: int) -> dict[str, Any]:
    _not_self_or_400(user_id)
    return {"deleted": users_store.delete_user(user_id)}


@app.post("/api/users/{user_id}/reset-password-link")
def send_user_reset_link(user_id: int) -> dict[str, Any]:
    auth.require_admin()
    user = users_store.get_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="unknown user")
    try:
        _mail_temp_password(user)
    except mailer.MailNotConfigured as exc:
        raise HTTPException(
            status_code=503, detail="chưa cấu hình gửi email (SMTP_HOST trong .env)",
        ) from exc
    except mailer.MailDeliveryFailed as exc:
        raise HTTPException(status_code=502, detail="không gửi được email") from exc
    return {"ok": True}


@app.post("/api/users/{user_id}/set-password")
def set_user_password(user_id: int, password: str = Body(..., embed=True)) -> dict[str, Any]:
    auth.require_admin()
    if users_store.get_by_id(user_id) is None:
        raise HTTPException(status_code=404, detail="unknown user")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="mật khẩu cần tối thiểu 8 ký tự")
    users_store.update_password(user_id, auth.hash_password(password))
    # An admin setting someone's password is usually a response to that
    # account being compromised; leaving its existing sessions alive would
    # defeat the exercise.
    users_store.invalidate_sessions(user_id)
    return {"ok": True}


def _account_or_404_admin(account_id: str):
    # Same lookup as _account_or_404 but without the caller's own access
    # check - an admin granting access to a workspace they can already see
    # everything of doesn't need the permission gate applied to itself.
    if registry.get(account_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown account: {account_id}")



# ------------------------------------------------------------- workspace logs
def _log_dir(account_id: str) -> Path:
    return BASE_DIR / "data" / "accounts" / account_id / "logs"


@app.get("/api/logs/{account_id}")
def read_workspace_log(
    account_id: str,
    file: str = Query("runtime"),
    lines: int = Query(500, ge=1, le=logs.MAX_ENTRIES),
    level: str | None = Query(None),
    q: str = Query("", max_length=200),
) -> dict[str, Any]:
    """Tail one of a workspace's process logs. Admin only, for every workspace.

    Logs carry customer names and conversation ids of whichever shop wrote
    them, so this is not behind `_account_or_404` (which would also let a
    granted employee in): only an admin may read them. `file` is a key of
    `logs.LOG_FILES`, never a path.
    """
    auth.require_admin()
    _account_or_404_admin(account_id)
    filename = logs.LOG_FILES.get(file)
    if filename is None:
        raise HTTPException(status_code=400, detail=f"unknown log file: {file}")
    if level is not None and level.upper() not in logs.LEVELS:
        raise HTTPException(status_code=400, detail=f"unknown level: {level}")
    log_dir = _log_dir(account_id)
    path = log_dir / filename
    result = (
        logs.tail(path, limit=lines, min_level=level, query=q)
        if path.is_file()
        else {"entries": [], "size": 0, "truncated": False}
    )
    return {"file": file, "files": logs.available_files(log_dir), **result}

# ------------------------------------------------------------- system-level
@app.get("/api/system")
def read_system() -> dict[str, Any]:
    """What the dashboard needs to know about this host - and nothing more.

    This used to be public and used to include `chrome_binary` and `base_dir`,
    i.e. absolute filesystem paths of the install, plus a count of every
    workspace on the box regardless of who owns it. It is now behind the
    session middleware and says only whether Chrome is usable at all.
    """
    user = auth.require_current_user()
    accounts = registry.list() if user["is_admin"] else user["account_ids"]
    return {
        "chrome_available": bool(find_chrome()),
        "accounts": len(accounts),
    }


# --------------------------------------------------------------- noVNC gate
# Mirrors the nginx `location ~ ^/vnc/<id>/...` pattern. Kept strict and
# anchored: this string decides which workspace's screen someone is about to
# be shown, so anything it does not recognise must fail closed rather than
# fall through to a default.
_VNC_URI = re.compile(r"^/vnc/(?P<account_id>[A-Za-z0-9._-]+)/")


@app.get("/api/vnc-auth")
def vnc_auth(response: Response, x_original_uri: str = Header("")) -> dict[str, Any]:
    """Authorize one noVNC request on nginx's behalf (its `auth_request`).

    nginx calls this before proxying anything under `/vnc/<account_id>/` to
    that workspace's loopback-only websockify, and reads the port to use off
    the `X-Novnc-Port` response header - so a workspace created after nginx
    started needs no config change.

    Why this exists at all: VNC used to sit outside every permission the rest
    of the dashboard has. Reaching the port *was* the authorization, which
    meant anyone who could reach loopback (an `ssh -L`, a tailnet) saw every
    workspace's screen regardless of which ones they were granted. Going
    through `_account_or_404` puts it back under the same rule as every other
    account-scoped route: the invariant in CLAUDE.md about never crossing
    workspaces now covers the screen too, and a revoked grant closes the
    screen on the very next request without anyone remembering to.

    Three answers, and nginx treats them differently:
      * 2xx      -> proxy the request
      * 401/403  -> refuse with that status (no session; or no access)
      * anything else -> nginx turns it into a 500
    That last rule is why this only ever answers the *permission* question.
    Whether the workspace is actually up is left to the proxy hop, which
    fails with a plain 502 - a stopped workspace is not an authorization
    error, and dressing it up as one would make every real 500 ambiguous.

    The method is GET and the body is dropped by nginx, so nothing here may
    have side effects: it runs again for every static asset of the noVNC page
    and again for the WebSocket upgrade.
    """
    match = _VNC_URI.match(x_original_uri or "")
    if match is None:
        # nginx should never send anything else, so this is either a
        # misconfiguration or someone calling the endpoint directly.
        raise HTTPException(status_code=403, detail="not a vnc request")
    # 404 for unknown, 403 without a grant, 401 earlier in the middleware if
    # there is no session at all.
    account = _account_or_404(match.group("account_id"))
    response.headers["X-Novnc-Port"] = str(account.novnc_port)
    return {"ok": True}


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    """Liveness probe. Outside /api/, so the auth middleware lets it through.

    Note this is not reachable through the dashboard nginx, which proxies only
    /api/ and serves the SPA for everything else - that is intended. Probes
    (systemd, a monitor, cloudflared) call the supervisor port directly.
    """
    return {"ok": True}


@app.get("/api/accounts")
def list_accounts() -> list[dict[str, Any]]:
    user = auth.require_current_user()
    accounts = registry.list()
    if not user["is_admin"]:
        allowed = set(user["account_ids"])
        accounts = [account for account in accounts if account.id in allowed]
    # Owner email is only resolved (and only worth resolving) for the admin
    # view, where it drives grouping workspaces by who created them - a
    # non-admin's own workspaces already tell them who owns them (them).
    owner_emails: dict[int, str] = {}
    if user["is_admin"]:
        owner_emails = {row["id"]: row["email"] for row in users_store.list_users()}
    result = []
    for account in accounts:
        managed = processes.get(account.id)
        result.append({
            **account.as_dict(),
            "owner_email": owner_emails.get(account.owner_id) if account.owner_id else None,
            "processes": managed.snapshot() if managed else {
                "chrome": {"running": False, "pid": None},
                "agent": {"running": False, "pid": None, "runtime": None},
            },
            "control_connected": hub.is_connected(account.id),
            "database": _reader(account).health(),
            **sleep_wake.snapshot(account.id),
        })
    return result


@app.post("/api/accounts")
def create_account(
    account_id: str = Body("", embed=True),
    label: str = Body("", embed=True),
    profile_dir_name: str = Body("", embed=True),
    api_key: str = Body("", embed=True),
) -> dict[str, Any]:
    user = auth.require_current_user()
    try:
        # Count and create under one lock. The quota is a check-then-act
        # ("how many do they own?" then "make another"), and `registry.list`
        # takes no lock of its own - so without this, two requests arriving
        # together both read the same pre-write count and both slip past a
        # limit of 1. RLock is reentrant, so registry.create re-acquiring it
        # inside is fine.
        with registry.lock:
            # Creating a workspace is not a free action: it writes a YAML
            # config and a registry entry, and the very next thing anyone
            # does with it (POST .../start) spawns a Chrome and an agent
            # process on this host. A non-admin may do it only within the
            # quota an admin granted them, which is 0 until an admin says
            # otherwise (workspace_limit above).
            #
            # Admins are never capped - the limit exists so an admin can
            # ration workspaces per operator, not to ration the admin.
            # Checked before registry.create so a refused request leaves no
            # half-made workspace (create writes the YAML + registry file
            # immediately).
            if not user["is_admin"]:
                limit = workspace_limit(user)
                owned = _owned_workspace_count(user["id"])
                if owned >= limit:
                    raise HTTPException(
                        status_code=403,
                        detail=(
                            f"Tài khoản này chỉ được tạo tối đa {limit} workspace "
                            f"(đang có {owned}). Liên hệ admin để nâng giới hạn."
                        ),
                    )
            account = registry.create(
                account_id, label,
                profile_dir_name=profile_dir_name, api_key=api_key,
                owner_id=user["id"],
            )
    except InvalidAccountName as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not user["is_admin"]:
        # Admins already see every workspace unconditionally (list_accounts
        # above); a non-admin creator needs an explicit grant or their own
        # new workspace would vanish from their next GET /api/accounts.
        users_store.grant_access(user["id"], account.id)
    return account.as_dict()


@app.patch("/api/accounts/{account_id}")
def update_account(
    account_id: str,
    label: str | None = Body(None, embed=True),
    api_key: str | None = Body(None, embed=True),
    egw_token: str | None = Body(None, embed=True),
) -> dict[str, Any]:
    """Edit label/API key after creation - e.g. filling in the Gemini key
    later instead of requiring it at workspace-creation time. Does not touch
    the running process: an already-started agent keeps its old key until
    Stop then Start (the key is injected as an env var at process start, see
    AccountProcesses._agent_env), same restart_required pattern as other
    boot-time config.

    `egw_token` here is the hand-paste path (token issued with `egw token
    issue` on the gateway host); it clears any route/expected_ip recorded by
    an earlier API provisioning, since a pasted token says nothing about
    which route it belongs to. `POST /api/accounts/{id}/egw` is the
    provisioned path that fills all three.
    """
    _owned_account_or_404(account_id)
    egw_fields: dict[str, str] = {}
    if egw_token is not None:
        egw_fields = {"egw_token": egw_token, "egw_route": "", "egw_expected_ip": ""}
    try:
        account = registry.update(account_id, label=label, api_key=api_key, **egw_fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if account is None:
        raise HTTPException(status_code=404, detail=f"unknown account: {account_id}")
    return account.as_dict()


@app.delete("/api/accounts/{account_id}")
async def delete_account(
    account_id: str, force: bool = Query(False), purge: bool = Query(False),
) -> dict[str, Any]:
    account = _owned_account_or_404(account_id)
    managed = processes.get(account_id)
    tracked_running = bool(managed and (managed.agent.running or managed.chrome.running))
    # The control server can still be answering on account.control_port even
    # when this supervisor process never spawned it - e.g. it survived an
    # earlier supervisor restart (the same gap `dev.sh`'s cleanup now closes
    # for the happy path). Deleting the registry entry out from under a
    # still-alive, untracked agent would silently orphan it, so refuse
    # instead of guessing at a PID to kill.
    if hub.is_connected(account_id) and not tracked_running and not force:
        raise HTTPException(
            status_code=409,
            detail=(
                "Agent của workspace này vẫn đang phản hồi nhưng không do "
                "phiên supervisor hiện tại quản lý (có thể còn sống từ một "
                "lần chạy trước). Hãy dừng nó rồi thử lại."
            ),
        )
    if managed is not None:
        await managed.stop_all(include_chrome=True)
        processes.pop(account_id, None)
    hub.unfollow(account_id)
    readers.pop(account_id, None)
    product_repos.pop(account_id, None)
    if account.egw_token:
        # Best-effort: an unreachable egw must not keep a workspace alive.
        await egw.revoke(account_id)
    return {"deleted": registry.delete(account_id, purge=purge)}


# ------------------------------------------------------------------ egw
@app.get("/api/egw/routes")
async def egw_routes() -> dict[str, Any]:
    """Routes (public IPs) the operator can pick from when enabling egw for a
    workspace. `configured: false` means this machine has no egw wired up -
    the UI hides the feature rather than showing an error."""
    auth.require_current_user()
    if not egw.configured():
        return {"configured": False, "routes": []}
    try:
        routes = await egw.list_routes()
    except egw.EgwError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Không kết nối được egw Admin API ({type(exc).__name__}) - egw có đang chạy?",
        ) from exc
    return {"configured": True, "routes": routes}


@app.post("/api/accounts/{account_id}/egw")
async def enable_egw(account_id: str, route: str = Body(..., embed=True)) -> dict[str, Any]:
    """Map this workspace onto `route` on egw, issue its token, and record
    token/route/expected IP. The proxy takes effect at the workspace's next
    Start (Chrome flags and the agent's YAML are read at spawn)."""
    account = _owned_account_or_404(account_id)
    route = route.strip()
    if not route:
        raise HTTPException(status_code=400, detail="Chưa chọn route")
    try:
        result = await egw.provision(account_id, route, current_holder=account.egw_route)
    except egw.EgwNotConfigured as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except egw.EgwError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Không kết nối được egw Admin API ({type(exc).__name__}) - egw có đang chạy?",
        ) from exc
    account = registry.update(
        account_id, egw_token=result["token"], egw_route=result["route"],
        egw_expected_ip=result["expected_ip"],
    )
    managed = processes.get(account_id)
    running = bool(managed and (managed.agent.running or managed.chrome.running))
    return {**account.as_dict(), "restart_required": running}


@app.delete("/api/accounts/{account_id}/egw")
async def disable_egw(account_id: str) -> dict[str, Any]:
    """Revoke the token on egw and clear it here. The workspace goes back to
    the host's own IP at its next Start."""
    account = _owned_account_or_404(account_id)
    if account.egw_token:
        await egw.revoke(account_id)
    account = registry.update(account_id, egw_token="", egw_route="", egw_expected_ip="")
    managed = processes.get(account_id)
    running = bool(managed and (managed.agent.running or managed.chrome.running))
    return {**account.as_dict(), "restart_required": running}


# ------------------------------------------------------- process lifecycle
@app.post("/api/accounts/{account_id}/start")
async def start_account(
    account_id: str,
    runtime: str = Body("langgraph", embed=True),
    with_chrome: bool = Body(True, embed=True),
) -> dict[str, Any]:
    account = _account_or_404(account_id)
    registry.write_config(account)          # keep the config in step with ports
    # Operator-initiated: give the sleep/wake sweeper a clean baseline instead
    # of letting it act on lifecycle state left over from before this Start.
    sleep_wake.reset(account_id)
    managed = _procs(account, runtime)
    managed.runtime = runtime
    if with_chrome:
        await managed.start_chrome()
        # Chrome needs a moment before its CDP port accepts connections; the
        # agent's own preflight reports a clean error if it is still not up.
        await asyncio.sleep(2.0)
    await managed.start_agent()
    hub.follow(account_id, account.control_port)
    hub.emit(account_id, "agent.status", status="starting", runtime=runtime)
    return managed.snapshot()


@app.post("/api/accounts/{account_id}/stop")
async def stop_account(
    account_id: str,
    include_chrome: bool = Body(True, embed=True),
) -> dict[str, Any]:
    account = _account_or_404(account_id)
    managed = _procs(account)
    result = await managed.stop_all(include_chrome=include_chrome)
    hub.unfollow(account_id)
    hub.emit(account_id, "agent.status", status="stopped", **result)
    # A manual Stop is an explicit operator decision, not idleness - the
    # sweeper must not later try to "wake" a workspace someone deliberately
    # stopped.
    sleep_wake.reset(account_id)
    return result


# --------------------------------------------------------------- sleep/wake
@app.post("/api/accounts/{account_id}/heartbeat")
async def workspace_heartbeat(account_id: str) -> dict[str, Any]:
    """Called by the dashboard's Agent page every ~15s while it is open, so
    the idle sweeper never puts a workspace an operator is actively looking
    at to sleep. Also wakes the workspace immediately if it happens to
    already be SLEEPING/DEGRADED - covers an operator navigating straight to
    a sleeping workspace's page without a separate manual wake call.
    """
    _account_or_404(account_id)
    sleep_wake.heartbeat(account_id)
    return {"ok": True}


@app.post("/api/accounts/{account_id}/pin")
async def pin_workspace(account_id: str, pinned: bool = Body(..., embed=True)) -> dict[str, Any]:
    """Operator 'Giữ thức' toggle - blocks the idle sweeper from sleeping this
    workspace regardless of idle time, until unpinned. Persisted (unlike the
    rest of the sleep/wake lifecycle) so it survives a supervisor restart.
    """
    _account_or_404(account_id)
    account = sleep_wake.set_pinned(account_id, pinned)
    if account is None:
        raise HTTPException(status_code=404, detail=f"unknown account: {account_id}")
    return {"pinned": account.pinned}


@app.post("/api/accounts/{account_id}/wake")
async def wake_workspace(account_id: str) -> dict[str, Any]:
    """Explicit manual wake - used by the dashboard when an operator opens or
    focuses a SLEEPING/DEGRADED workspace, instead of waiting for the next
    periodic wake-poll tick.
    """
    _account_or_404(account_id)
    await sleep_wake.wake(account_id, reason="manual")
    return sleep_wake.snapshot(account_id)


# ------------------------------------------------------------ agent control
@app.get("/api/accounts/{account_id}/status")
async def account_status(account_id: str):
    return await _control(_account_or_404(account_id), "GET", "/control/status")


@app.get("/api/accounts/{account_id}/health")
async def account_health(account_id: str):
    account = _account_or_404(account_id)
    checks = await _control(account, "GET", "/control/health")
    checks.setdefault("checks", {})["database"] = _reader(account).health()
    return checks


@app.post("/api/accounts/{account_id}/pause")
async def pause_account(account_id: str, reason: str = Body("operator", embed=True)):
    return await _control(_account_or_404(account_id), "POST",
                          "/control/pause", {"reason": reason})


@app.post("/api/accounts/{account_id}/resume")
async def resume_account(account_id: str):
    return await _control(_account_or_404(account_id), "POST", "/control/resume")


@app.post("/api/accounts/{account_id}/focus-window")
async def focus_workspace_window(account_id: str) -> dict[str, Any]:
    """Make sure this workspace's own Chrome/display stack is up, waking it
    from sleep if needed.

    Named for what it used to also do: each workspace shared one display with
    every other one, so opening its noVNC tab first needed this endpoint to
    raise its window in front of everyone else's (xdotool search/windowraise/
    windowfocus). Every workspace now has its own dedicated Xvfb - there is
    exactly one window on it, ever - so there is nothing left to raise; the
    URL is kept unchanged (dashboard/src/lib/api.ts's openVncWindow already
    calls this first) rather than breaking the frontend contract for a
    cosmetic rename.
    """
    _account_or_404(account_id)
    managed = processes.get(account_id)
    running = bool(managed and managed.chrome.running)
    if not running:
        # Most common cause today: the workspace is SLEEPING. Kick off a
        # manual wake and ask the operator to retry shortly rather than
        # failing outright - waking Chrome itself takes a few seconds
        # (start_chrome() + CDP wait), too long to block this request on.
        if managed is not None:
            # Keeps a strong reference via SleepWakeManager._spawn - a bare
            # create_task here would only be weakly referenced and could be
            # garbage-collected mid-wake, leaving the workspace stuck WAKING.
            sleep_wake._spawn(
                sleep_wake.wake(account_id, reason="manual"),
                name=f"wake-{account_id}-manual",
            )
        raise HTTPException(
            status_code=409,
            detail=(
                "Chrome của workspace này chưa chạy - nếu đang SLEEPING, hệ "
                "thống đang tự đánh thức, thử lại sau vài giây; nếu không, "
                "bấm Start trước."
            ),
        )
    return {"ok": True}


@app.post("/api/accounts/{account_id}/tasks")
async def submit_task(account_id: str, task: dict = Body(...)):
    return await _control(_account_or_404(account_id), "POST", "/control/tasks", task)


# The two numeric knobs on the "Nhịp quét & trả lời" card, with the range each
# may take. The page's min/max used to be HTML attributes only, so anything
# at all reached the agent - a 1-second scan interval included.
INTERVAL_KEY = "messaging.auto_check.interval_seconds"
QUIET_PERIOD_KEY = "messaging.auto_check.reply_quiet_period_seconds"
REPLY_WAIT_TIMEOUT_KEY = "messaging.auto_check.reply_wait_timeout_seconds"
PACING_LIMITS = {
    INTERVAL_KEY: (5, 600),
    QUIET_PERIOD_KEY: (0, 300),
}
PACING_LABELS = {
    INTERVAL_KEY: "Bao lâu quét inbox một lần",
    QUIET_PERIOD_KEY: "Chờ tin nhắn trong bao lâu",
}


def _pacing_limits(cfg) -> dict[str, dict[str, float]]:
    """Bounds for this workspace. The quiet period also has to stay below how
    long the agent is willing to sit in one conversation
    (`reply_wait_timeout_seconds`): at or above it, a burst can never go quiet
    in time, so every customer is pushed to a later scan and answered late -
    silently, the agent only ever warned about it in its boot log."""
    limits = {key: {"min": low, "max": high} for key, (low, high) in PACING_LIMITS.items()}
    timeout = cfg.get(REPLY_WAIT_TIMEOUT_KEY, sleep_config.get(REPLY_WAIT_TIMEOUT_KEY, 30))
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = 0
    if timeout > 0:
        limits[QUIET_PERIOD_KEY]["max"] = min(
            limits[QUIET_PERIOD_KEY]["max"], max(0, math.ceil(timeout) - 1),
        )
    return limits


def _pacing_error(limits: dict, key: str, value) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "phải là một số"
    low, high = limits[key]["min"], limits[key]["max"]
    if not low <= value <= high:
        return f"phải trong khoảng {low:g} - {high:g} giây"
    return None


@app.get("/api/accounts/{account_id}/settings")
def read_settings(account_id: str):
    """What the pacing knobs are set to now, and what they may be set to.

    Read from the workspace's own config file, so the page can show it while
    the agent is stopped. A running agent writes every change it accepts back
    to that same file (control/settings.py), so the file is also what it is
    using - except for the scan interval, which it only reads at start.
    """
    cfg = registry.config(_account_or_404(account_id))
    return {
        "values": {
            key: cfg.get(key, sleep_config.get(key)) for key in PACING_LIMITS
        },
        "limits": _pacing_limits(cfg),
    }


@app.patch("/api/accounts/{account_id}/settings")
async def patch_settings(account_id: str, values: dict = Body(..., embed=True)):
    account = _account_or_404(account_id)
    cfg = registry.config(account)
    limits = _pacing_limits(cfg)
    rejected: dict[str, str] = {}
    accepted: dict[str, Any] = {}
    for key, value in values.items():
        if key in PACING_LIMITS and (error := _pacing_error(limits, key, value)):
            rejected[key] = error
        else:
            accepted[key] = value

    managed = processes.get(account.id)
    if managed is not None and managed.agent.running:
        # A running agent has to be the one that writes: it holds the config
        # in memory and saves the whole of it on every change it accepts, so
        # a line written here behind its back would be overwritten by its
        # next save.
        if not accepted:
            return {"applied": {}, "restart_required": {}, "rejected": rejected}
        result = await _control(account, "PATCH", "/control/settings", {"values": accepted})
        result.setdefault("rejected", {}).update(rejected)
        return result

    # Stopped (or sleeping): nothing is holding the file, so write the pacing
    # knobs straight into it and they take effect when the agent next starts.
    # Anything else still needs a running agent to mean something.
    saved: dict[str, str] = {}
    for key, value in accepted.items():
        if key in PACING_LIMITS:
            cfg.set(key, value)
            saved[key] = "đã lưu - có hiệu lực khi workspace chạy"
        else:
            rejected[key] = "agent chưa chạy"
    if saved:
        cfg.save()
    return {"applied": {}, "restart_required": saved, "rejected": rejected}


@app.get("/api/accounts/{account_id}/scenario")
def read_scenario(account_id: str):
    """The operator's saved Khách/Agent example exchanges.

    Read straight from the account's own config file rather than through the
    control server: unlike the numeric knobs on `/settings`, this needs to be
    visible (and pre-fillable in the editor) even while the agent is stopped.
    """
    account = _account_or_404(account_id)
    cfg = registry.config(account)
    return {
        "examples": cfg.get("messaging.scenario_examples", []),
        **_reply_style_view(cfg),
        "reply_language": cfg.get("messaging.reply_language", DEFAULT_REPLY_LANGUAGE),
        # The picker's options, so the dashboard never hardcodes a list that
        # can drift from agent/reply_language.py.
        "reply_languages": [
            {"code": code, "label": language_label(code)} for code in REPLY_LANGUAGES
        ],
    }


@app.put("/api/accounts/{account_id}/scenario")
async def write_scenario(account_id: str, examples: list[dict] = Body(..., embed=True)):
    account = _account_or_404(account_id)
    if len(examples) > SCENARIO_MAX_EXAMPLES:
        raise HTTPException(
            status_code=422,
            detail=f"at most {SCENARIO_MAX_EXAMPLES} examples are allowed",
        )
    cleaned = []
    for item in examples:
        customer = str(item.get("customer", "")).strip()
        agent_reply = str(item.get("agent", "")).strip()
        if not customer or not agent_reply:
            raise HTTPException(
                status_code=422,
                detail="each example needs both a customer message and an agent reply",
            )
        if (
            len(customer) > SCENARIO_MAX_FIELD_CHARACTERS
            or len(agent_reply) > SCENARIO_MAX_FIELD_CHARACTERS
        ):
            raise HTTPException(
                status_code=422,
                detail=f"each field is limited to {SCENARIO_MAX_FIELD_CHARACTERS} characters",
            )
        cleaned.append({"customer": customer, "agent": agent_reply})
    # Persisted directly so it survives regardless of whether the agent is
    # running right now - a playbook the operator writes before ever starting
    # the workspace must not be lost. `Config.set`/`save` write straight to
    # this account's own YAML file, the same one `write_config` derives.
    cfg = registry.config(account)
    cfg.set("messaging.scenario_examples", cleaned)
    cfg.save()
    # Best-effort: if the agent happens to be running, push it live so the
    # very next reply already uses it instead of waiting for a restart.
    with contextlib.suppress(HTTPException):
        await _control(account, "PATCH", "/control/settings",
                       {"values": {"messaging.scenario_examples": cleaned}})
    return {"examples": cleaned}


def _reply_style_view(cfg) -> dict:
    """The style rules in effect, and whether they are the language's default.

    An unset `messaging.reply_style` follows the reply language's own default
    rules (agent/reply_language.py), so the editor shows those rather than an
    empty box that would read as "no rules at all".
    """
    rules = cfg.get("messaging.reply_style")
    if rules is None:
        language = cfg.get("messaging.reply_language", DEFAULT_REPLY_LANGUAGE)
        return {"reply_style": default_reply_style(language), "reply_style_is_default": True}
    return {"reply_style": list(rules), "reply_style_is_default": False}


@app.put("/api/accounts/{account_id}/reply-style")
async def write_reply_style(account_id: str, rules: list[str] = Body(..., embed=True)):
    """Tone/layout rules for every reply prompt, one rule per entry.

    Stored and pushed exactly like the scenario examples above: to the file
    first so a stopped workspace keeps it, then live to a running agent.
    """
    account = _account_or_404(account_id)
    cleaned = [" ".join(rule.split()) for rule in rules if rule.strip()]
    if len(cleaned) > REPLY_STYLE_MAX_RULES:
        raise HTTPException(
            status_code=422,
            detail=f"at most {REPLY_STYLE_MAX_RULES} style rules are allowed",
        )
    if any(len(rule) > REPLY_STYLE_MAX_RULE_CHARACTERS for rule in cleaned):
        raise HTTPException(
            status_code=422,
            detail=f"each rule is limited to {REPLY_STYLE_MAX_RULE_CHARACTERS} characters",
        )
    cfg = registry.config(account)
    language = cfg.get("messaging.reply_language", DEFAULT_REPLY_LANGUAGE)
    # Saving the default rules unchanged keeps following the language (null),
    # so a later language switch still brings that language's rules along.
    stored = None if cleaned == default_reply_style(language) else cleaned
    cfg.set("messaging.reply_style", stored)
    cfg.save()
    with contextlib.suppress(HTTPException):
        await _control(account, "PATCH", "/control/settings",
                       {"values": {"messaging.reply_style": stored}})
    return _reply_style_view(cfg)


@app.get("/api/accounts/{account_id}/shipping")
def read_shipping(account_id: str):
    """The shop's delivery facts (`products.shipping`, agent/shop_policy.py)."""
    account = _account_or_404(account_id)
    return {"shipping": registry.config(account).get("products.shipping")}


@app.put("/api/accounts/{account_id}/shipping")
async def write_shipping(account_id: str, shipping: dict | None = Body(..., embed=True)):
    """Stored and pushed like the reply style: file first, then live."""
    account = _account_or_404(account_id)
    try:
        cleaned = clean_shipping_setting(shipping)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    cfg = registry.config(account)
    cfg.set("products.shipping", cleaned)
    cfg.save()
    with contextlib.suppress(HTTPException):
        await _control(account, "PATCH", "/control/settings",
                       {"values": {"products.shipping": cleaned}})
    return {"shipping": cleaned}


@app.put("/api/accounts/{account_id}/reply-language")
async def write_reply_language(account_id: str, language: str = Body(..., embed=True)):
    """The language every reply is written in, whatever the customer wrote in.

    Stored and pushed exactly like the reply style above: to the file first so
    a stopped workspace keeps it, then live to a running agent.
    """
    account = _account_or_404(account_id)
    code = str(language or "").strip().lower()
    if code not in REPLY_LANGUAGES:
        raise HTTPException(
            status_code=422,
            detail=f"ngôn ngữ không hỗ trợ; chọn một trong: {', '.join(REPLY_LANGUAGES)}",
        )
    cfg = registry.config(account)
    cfg.set("messaging.reply_language", code)
    cfg.save()
    with contextlib.suppress(HTTPException):
        await _control(account, "PATCH", "/control/settings",
                       {"values": {"messaging.reply_language": code}})
    # A style that follows the language has just changed with it.
    return {"reply_language": code, **_reply_style_view(cfg)}


@app.get("/api/accounts/{account_id}/followups")
def read_followups(account_id: str):
    """The operator's saved "silent for N minutes -> send this" rules.

    Read straight from the account's own config file rather than through the
    control server: needs to be visible (and pre-fillable in the editor) even
    while the agent is stopped, same reasoning as the scenario endpoints above.
    """
    account = _account_or_404(account_id)
    return {"rules": registry.config(account).get("messaging.follow_up.rules", [])}


@app.put("/api/accounts/{account_id}/followups")
async def write_followups(account_id: str, rules: list[dict] = Body(..., embed=True)):
    account = _account_or_404(account_id)
    if len(rules) > FOLLOWUP_MAX_RULES:
        raise HTTPException(
            status_code=422, detail=f"at most {FOLLOWUP_MAX_RULES} rules are allowed",
        )
    cleaned = []
    seen_ids: set[str] = set()
    for item in rules:
        message = str(item.get("message", "")).strip()
        if not message:
            raise HTTPException(status_code=422, detail="each rule needs a message to send")
        if len(message) > FOLLOWUP_MAX_MESSAGE_CHARACTERS:
            raise HTTPException(
                status_code=422,
                detail=f"message is limited to {FOLLOWUP_MAX_MESSAGE_CHARACTERS} characters",
            )
        try:
            delay_minutes = int(item.get("delay_minutes"))
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=422, detail="delay_minutes must be a whole number of minutes",
            ) from None
        if not (FOLLOWUP_MIN_DELAY_MINUTES <= delay_minutes <= FOLLOWUP_MAX_DELAY_MINUTES):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"delay_minutes must be between {FOLLOWUP_MIN_DELAY_MINUTES} and "
                    f"{FOLLOWUP_MAX_DELAY_MINUTES}"
                ),
            )
        # A stable id survives edits to other rules; a fresh one only for a
        # genuinely new rule (or a collided/missing one from the client).
        rule_id = str(item.get("id") or "").strip() or uuid.uuid4().hex[:12]
        if rule_id in seen_ids:
            rule_id = uuid.uuid4().hex[:12]
        seen_ids.add(rule_id)
        cleaned.append({
            "id": rule_id, "delay_minutes": delay_minutes, "message": message,
            "enabled": bool(item.get("enabled", True)),
        })
    # Persisted directly so it survives regardless of whether the agent is
    # running right now, same as the scenario endpoints above.
    cfg = registry.config(account)
    cfg.set("messaging.follow_up.rules", cleaned)
    cfg.save()
    with contextlib.suppress(HTTPException):
        await _control(account, "PATCH", "/control/settings",
                       {"values": {"messaging.follow_up.rules": cleaned}})
    return {"rules": cleaned}


# --------------------------------------------------------------- data reads
# Plain `def` handlers: FastAPI runs them in a threadpool, so blocking database
# reads never stall the event loop that is serving the WebSocket.
@app.get("/api/accounts/{account_id}/summary")
def read_summary(account_id: str):
    return _reader(_account_or_404(account_id)).counts()


@app.get("/api/accounts/{account_id}/tasks")
def read_tasks(account_id: str, status: str | None = None,
               capability: str | None = None,
               limit: int = Query(50, le=500), offset: int = 0):
    return _reader(_account_or_404(account_id)).tasks(
        status=status, capability=capability, limit=limit, offset=offset
    )


@app.get("/api/accounts/{account_id}/tasks/{task_id}")
def read_task(account_id: str, task_id: str):
    task = _reader(_account_or_404(account_id)).task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="unknown task")
    return task


@app.get("/api/accounts/{account_id}/activity")
def read_activity(account_id: str, limit: int = Query(100, le=500)):
    return _reader(_account_or_404(account_id)).activity(limit=limit)


@app.get("/api/accounts/{account_id}/escalations")
def read_escalations(account_id: str, status: str = "OPEN",
                     limit: int = Query(50, le=200)):
    return _reader(_account_or_404(account_id)).escalations(status=status, limit=limit)


@app.get("/api/accounts/{account_id}/conversations")
def read_conversations(account_id: str, limit: int = Query(100, le=500)):
    return _reader(_account_or_404(account_id)).conversations(limit=limit)


# ------------------------------------------------------------- global search (⌘K)
_SEARCH_CACHE_TTL_SECONDS = 10.0
_search_candidate_cache: dict[str, dict[str, Any]] = {}
_search_cache_lock = asyncio.Lock()


def clear_search_cache(account_id: str | None = None) -> None:
    """Evict cached search candidates. Useful in tests and after mutations."""
    if account_id is None:
        _search_candidate_cache.clear()
    else:
        _search_candidate_cache.pop(account_id, None)


async def _get_search_candidates(account, reader) -> dict[str, Any]:
    now = time.monotonic()
    entry = _search_candidate_cache.get(account.id)
    if entry is not None and now - entry["timestamp"] < _SEARCH_CACHE_TTL_SECONDS:
        return entry

    async with _search_cache_lock:
        now = time.monotonic()
        entry = _search_candidate_cache.get(account.id)
        if entry is not None and now - entry["timestamp"] < _SEARCH_CACHE_TTL_SECONDS:
            return entry

        # Fetch in parallel: conversations (300), orders (300), products
        convs_task = asyncio.to_thread(reader.conversations, 300)
        orders_task = asyncio.to_thread(orders_store.list, account.id, 300)
        prods_task = asyncio.to_thread(_products(account).load_products)

        convs, orders, prods = await asyncio.gather(convs_task, orders_task, prods_task)

        entry = {
            "timestamp": now,
            "conversations": convs,
            "orders": orders,
            "products": prods,
            "recent_messages": None,
        }
        _search_candidate_cache[account.id] = entry
        return entry


@app.get("/api/accounts/{account_id}/search")
async def search_account(account_id: str, q: str = Query("", max_length=200),
                         limit: int = Query(6, ge=1, le=20)) -> dict[str, Any]:
    """Global search (the dashboard's ⌘K palette) over one workspace.

    Accent-insensitive on purpose (dashboard_api/search.py): operators type
    "nguyen" and expect "Nguyễn". Each group is capped at `limit` so the
    palette stays a palette; the Inbox/Orders/Products pages remain the
    place to browse everything.
    """
    account = _account_or_404(account_id)
    term = q.strip()
    empty = {"customers": [], "orders": [], "products": []}
    if len(term) < 2:
        return empty

    words = fold(term).split()
    if not words:
        return empty

    reader = _reader(account)
    candidates = await _get_search_candidates(account, reader)

    # Customers: by who they are (name/handle/id/phone) first, then by what
    # was said. A conversation that matched on identity is not listed a
    # second time for a message hit.
    customers: list[dict[str, Any]] = []
    seen: set[str] = set()
    recent = candidates["conversations"]
    for row in recent:
        if matches_words(words, row.get("display_name"), row.get("username"),
                         row.get("customer_id"), row.get("phone")):
            seen.add(row["conversation_id"])
            customers.append({
                "conversation_id": row["conversation_id"],
                "display_name": row.get("display_name"),
                "username": row.get("username"),
                "customer_id": row.get("customer_id"),
                "phone": row.get("phone"),
                "last_message_at": row.get("last_message_at"),
                "matched": "identity",
                "snippet": None,
            })
            if len(customers) >= limit:
                break
    if len(customers) < limit:
        messages = candidates.get("recent_messages")
        if messages is None:
            messages = await asyncio.to_thread(reader.recent_messages, 300)
            candidates["recent_messages"] = messages
        names = {
            row["conversation_id"]: row.get("display_name") or row.get("username")
            for row in recent
        }
        for message in messages:
            conversation_id = message["conversation_id"]
            if conversation_id in seen or not matches_words(words, message.get("text")):
                continue
            seen.add(conversation_id)
            customers.append({
                "conversation_id": conversation_id,
                "display_name": names.get(conversation_id),
                "username": None,
                "customer_id": None,
                "phone": None,
                "last_message_at": message.get("created_at"),
                "matched": "message",
                "snippet": snippet(message.get("text") or "", words),
            })
            if len(customers) >= limit:
                break

    orders = [
        {
            "id": order["id"], "customer_name": order["customer_name"],
            "status": order["status"], "total_amount": order.get("total_amount"),
            "items": order.get("items") or "", "created_at": order.get("created_at"),
        }
        for order in candidates["orders"]
        # "#7" is how an order is written everywhere on the Orders page, and
        # it also gets a one-digit id past the two-character minimum above.
        if matches_words(words, f"#{order['id']}", order.get("customer_name"),
                         order.get("items"), order.get("shipping_address"),
                         order.get("note"), order.get("conversation_id"))
    ][:limit]

    products = [
        {
            "id": product.get("id"), "name": product.get("name"),
            "price": product.get("price"), "stock": product.get("stock"),
            "is_active": product.get("is_active", True),
        }
        for product in candidates["products"]
        if matches_words(words, product.get("id"), product.get("name"), product.get("brand"),
                         product.get("category"), " ".join(product.get("keywords") or []),
                         " ".join(product.get("tags") or []))
    ][:limit]

    return {"customers": customers, "orders": orders, "products": products}



@app.get("/api/accounts/{account_id}/conversations/{conversation_id:path}")
def read_conversation(account_id: str, conversation_id: str,
                      limit: int = Query(200, le=1000)):
    return _reader(_account_or_404(account_id)).conversation(conversation_id, limit=limit)


# --------------------------------------------------------- conversation control
@app.post("/api/accounts/{account_id}/conversations/{conversation_id:path}/takeover")
async def takeover_conversation(account_id: str, conversation_id: str,
                                operator_id: str = Body("local_operator", embed=True)):
    account = _account_or_404(account_id)
    row, wrote_directly = await _control_or_write(
        account,
        f"/control/conversations/{quote(conversation_id, safe='')}/takeover",
        {"operator_id": operator_id},
        lambda writer: writer.takeover(conversation_id, operator_id),
    )
    if wrote_directly:
        # The agent publishes this event itself on the normal path. On the
        # fallback path nobody would, and the dashboard would keep showing the
        # old mode until its next poll.
        hub.emit(
            account_id, "conversation.takeover",
            conversation_id=conversation_id, operator_id=operator_id,
        )
    return row


@app.post("/api/accounts/{account_id}/conversations/{conversation_id:path}/release")
async def release_conversation(account_id: str, conversation_id: str):
    account = _account_or_404(account_id)
    row, wrote_directly = await _control_or_write(
        account,
        f"/control/conversations/{quote(conversation_id, safe='')}/release",
        None,
        lambda writer: writer.release(conversation_id),
    )
    if wrote_directly:
        hub.emit(
            account_id, "conversation.released", conversation_id=conversation_id,
        )
    return row


# What platforms/tiktok/messaging.py's send skill accepts.
MANUAL_MESSAGE_MAX_CHARACTERS = 1000


@app.post("/api/accounts/{account_id}/conversations/{conversation_id:path}/messages")
async def send_conversation_message(
    account_id: str, conversation_id: str,
    text: str = Body("", embed=True),
    client_message_id: str = Body(..., embed=True),
    operator_id: str = Body("local_operator", embed=True),
    attachments: list = Body([], embed=True),
):
    account = _account_or_404(account_id)
    # Ids from POST /chat-attachments; checked here, before anything is queued.
    attached = chat_media.resolve_attachments(account.id, attachments) if attachments else []
    if not text.strip() and not attached:
        raise HTTPException(status_code=400, detail="Nhập tin nhắn hoặc chọn tệp để gửi")
    if len(text.strip()) > MANUAL_MESSAGE_MAX_CHARACTERS:
        # The send skill refuses it inside the agent; saying so here, before
        # anything is queued, is what tells the person why.
        raise HTTPException(
            status_code=400,
            detail=f"Tin nhắn tối đa {MANUAL_MESSAGE_MAX_CHARACTERS} ký tự - chia thành nhiều tin",
        )
    detail = _reader(account).conversation(conversation_id)
    # Renewing settles the takeover TTL exactly as the agent's send gate will.
    # A person sending by hand IS taking the conversation over: when it is
    # not (or no longer) theirs - the takeover lapsed, or the agent handed it
    # over (WAITING_HUMAN) and nobody clicked Tiếp quản yet - it is taken over
    # here, the same way the button does, instead of refusing the message.
    # The agent asks for the mode again right before it sends anything, so it
    # cannot answer over this person from here on.
    control = await _control_writer(account).renew_human(conversation_id)
    if (control or {}).get("mode", "AUTO") != "HUMAN":
        control = await takeover_conversation(account_id, conversation_id, operator_id)
        if (control or {}).get("mode") != "HUMAN":
            raise HTTPException(
                status_code=409,
                detail="Không tiếp quản được hội thoại này - thử lại sau giây lát",
            )
    profile = detail.get("profile") or {}
    recipient = profile.get("username") or profile.get("display_name") or conversation_id
    # Deterministic id: a retried POST with the same client_message_id must
    # not create a second send task. TaskRepository.submit has no ON
    # CONFLICT, so the control server's submit closure checks for an
    # existing row before inserting.
    task_id = f"human-{conversation_id}-{client_message_id}"
    result = await _control(account, "POST", "/control/tasks", {
        "capability": "send_message",
        "task_id": task_id,
        # The agent runs one task at a time on its single Chrome session
        # (core/graph.py's run loop), claiming the next queued task ordered
        # by priority DESC then created_at ASC (storage/repositories.py) -
        # a manual reply cannot interrupt whatever DOM action is already
        # mid-flight, but a priority far above the default 5 that every
        # routine task (scan_inbox, auto-reply, ...) uses guarantees this
        # jumps ahead of anything else still queued, so it is claimed the
        # instant the in-flight task finishes instead of waiting its turn.
        "priority": 100,
        "payload": {
            "conversation_id": conversation_id,
            "recipient": recipient,
            "response": text,
            "attachments": attached,
            "confirm_send": True,
            "origin": "human",
            "operator_id": operator_id,
            "client_message_id": client_message_id,
            "inbound_watermark": client_message_id,
        },
    })
    # DOM-verified completion happens asynchronously inside the agent
    # process and fires its own "conversation.message.sent" event once the
    # message is actually persisted (see core/graph.py's
    # AgentGraph._graph_finalize) - that one is what makes the sent message
    # itself appear. This early, synthetic event just nudges the dashboard
    # to refetch right away so pending/sending UI state isn't stuck waiting
    # on the next poll while the send is still in flight.
    hub.emit(
        account_id, "conversation.message.sent",
        conversation_id=conversation_id, origin="human",
    )
    return result


# -------------------------------------------------------------- google sheet
# Only a Google Sheet link is handed to the page: the value lands in a button
# every user clicks, so a typo'd or hostile `.env` value must not become an
# arbitrary URL (or a `javascript:` one) opened from the dashboard.
_GSHEET_URL_PATTERN = re.compile(
    r"https://docs\.google\.com/spreadsheets/d/[A-Za-z0-9_-]+([/?#][^\s]*)?"
)


@app.get("/api/sheet-link")
def sheet_link() -> dict[str, str]:
    """The one Google Sheet the export writes to (GSHEET_URL in `.env`), for
    the Orders page's "Xuất Google Sheet" button. "" hides the button.

    One sheet for every workspace, by decision: everyone signed in to the
    dashboard can open it, whichever workspaces they were given. Read at call
    time, like security_config, so tests can patch the environment.
    """
    url = os.environ.get("GSHEET_URL", "").strip()
    if url and not _GSHEET_URL_PATTERN.fullmatch(url):
        logger.warning("[sheet] GSHEET_URL is not a Google Sheet link; button hidden")
        url = ""
    return {"url": url}


# ------------------------------------------------------------ notifications
@app.get("/api/notifications")
def read_notifications(status: str = "OPEN", account_id: str | None = None,
                       limit: int = Query(100, le=500)):
    user = auth.require_current_user()
    if account_id:
        auth.check_account_access(account_id)
        return notifications.list(status=status, account_id=account_id, limit=limit)
    if user["is_admin"]:
        return notifications.list(status=status, account_id=None, limit=limit)
    rows: list[dict] = []
    for allowed_id in user["account_ids"]:
        rows.extend(notifications.list(status=status, account_id=allowed_id, limit=limit))
    rows.sort(key=lambda row: row["created_at"], reverse=True)
    return rows[:limit]


@app.post("/api/notifications/{notification_id}/resolve")
def resolve_notification(notification_id: int):
    row = notifications.get(notification_id)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown notification")
    auth.check_account_access(row["account_id"])
    return {"resolved": notifications.resolve(notification_id)}


# ------------------------------------------------------------------- orders
app.include_router(order_routes.router)


# ------------------------------------------------------------- orchestration
async def _submit_for_plan(account_id: str, capability: str, payload: dict) -> str:
    account = _account_or_404(account_id)
    result = await _control(account, "POST", "/control/tasks",
                            {"capability": capability, "payload": payload})
    return result["task_id"]


async def _task_status_for_plan(account_id: str, task_id: str):
    account = registry.get(account_id)
    if account is None:
        return None
    return await asyncio.to_thread(_reader(account).task, task_id)


orchestrator = Orchestrator(
    submit=_submit_for_plan,
    task_status=_task_status_for_plan,
    emit=lambda account_id, event_type, **payload: hub.emit(
        account_id, event_type, **payload
    ),
)


MAX_RUN_STEPS = 50


@app.post("/api/runs")
async def start_run(mode: str = Body("parallel", embed=True),
                    steps: list[dict] = Body(..., embed=True)):
    """Submit a multi-workspace run.

    Every step is authorized here, synchronously, before the plan exists. The
    per-step `_account_or_404` inside the background task does still fire (the
    task inherits a copy of this request's context), but by then the caller
    already has a 200 and a plan id, and the rejection only shows up as a
    failed step. Worse, that copied context is a snapshot: a long sequential
    plan would keep running under a user whose access was revoked minutes ago.
    Checking up front is what makes "you may not touch that workspace" an
    answer to the request rather than a footnote in the result.
    """
    user = auth.require_current_user()
    if not steps:
        raise HTTPException(status_code=400, detail="a run needs at least one step")
    if len(steps) > MAX_RUN_STEPS:
        raise HTTPException(
            status_code=400,
            detail=f"tối đa {MAX_RUN_STEPS} bước trong một run",
        )
    running = orchestrator.running_plans_for(user["id"])
    if running >= MAX_RUNNING_PLANS_PER_OWNER:
        raise HTTPException(
            status_code=429,
            detail="quá nhiều run đang chạy; chờ một run kết thúc rồi thử lại",
        )
    for step in steps:
        if not isinstance(step, dict):
            raise HTTPException(status_code=400, detail="mỗi bước phải là một object")
        account_id = str(step.get("account_id") or "").strip()
        capability = str(step.get("capability") or "").strip()
        if not account_id or not capability:
            raise HTTPException(
                status_code=400, detail="mỗi bước cần account_id và capability",
            )
        if not isinstance(step.get("payload") or {}, dict):
            raise HTTPException(status_code=400, detail="payload phải là một object")
        # Raises 404 for an unknown workspace and 403 for one this user may
        # not touch - the same gate every other account-scoped route uses.
        _account_or_404(account_id)
    plan = orchestrator.start(mode, steps, owner_id=user["id"])
    return plan.as_dict()


def _plan_visible_to_current_user(plan) -> bool:
    """A non-admin sees/controls a plan only if every one of its steps is a
    workspace they have access to - a multi-account plan mixing an allowed
    and a disallowed account must not leak either the disallowed step's
    presence or let it be cancelled by someone who cannot see it.

    A plan also belongs to whoever submitted it; the step rule remains the
    fallback for plans created before `owner_id` existed."""
    user = auth.require_current_user()
    if user["is_admin"]:
        return True
    if plan.owner_id is not None:
        return plan.owner_id == user["id"]
    allowed = set(user["account_ids"])
    return all(step.account_id in allowed for step in plan.steps)


@app.get("/api/runs")
def list_runs():
    return [plan.as_dict() for plan in
            sorted(orchestrator.plans.values(), key=lambda p: p.created_at, reverse=True)
            if _plan_visible_to_current_user(plan)]


@app.post("/api/runs/{plan_id}/cancel")
def cancel_run(plan_id: str):
    plan = orchestrator.plans.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="unknown run")
    if not _plan_visible_to_current_user(plan):
        raise HTTPException(status_code=403, detail="no access to this run")
    return {"cancelled": orchestrator.cancel(plan_id)}


# ------------------------------------------------------------------ products
app.include_router(product_routes.router)
app.include_router(chat_media.router)
app.include_router(translation_routes.router)
app.include_router(push_routes.router)


@app.get("/api/accounts/{account_id}/conversation-media")
def read_conversation_media(account_id: str, path: str = Query(...)):
    """Serve a picture referenced from Inbox history, from this server's disk.

    Two roots hold one, and `path` must resolve inside one of them:

    * the account's product catalogue, for what the agent sent
      (`agent.reply_service.record_sent` only ever records `Attachment`s that
      `_verified_attachments` picked from there);
    * the account's media cache, for what `services.media_cache.MediaCache`
      downloaded out of the thread - TikTok's own URLs expire after about a
      month and its DM-private host answers 403 without the session cookies,
      so a customer's picture is only displayable from a local copy.

    `path` arrives from `media_json`, which is written from the page's DOM, so
    it is treated as untrusted: it is resolved and then required to be
    contained by a root, exactly as `read_product_media` does above.
    """
    account = _account_or_404(account_id)
    roots = [
        _products(account).product_root.resolve(),
        get_account_media_dir(account_id, create=False).resolve(),
        # Files an operator attached in the chat (routes/chat_media.py).
        get_account_upload_dir(account_id, create=False).resolve(),
    ]
    candidate = Path(path).resolve()
    if not any(_contained_by(candidate, root) for root in roots):
        raise HTTPException(status_code=404, detail="not found")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(candidate)


def _contained_by(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


# --------------------------------------------------------------- event feed
@app.websocket("/api/events")
async def events(websocket: WebSocket):
    # Starlette's HTTP middleware never sees WebSocket upgrades, so every gate
    # the middleware stack applies to /api/ has to be repeated here by hand:
    # Cloudflare Access, then Origin, then the session cookie.
    if not await cf_access.websocket_allowed(websocket):
        await websocket.close(code=1008)
        return
    # SameSite=Lax does not cover the WebSocket handshake - a page on another
    # origin can open one and the browser sends the session cookie with it.
    if not auth.origin_allowed(websocket.headers):
        logger.warning(
            "[events] rejected upgrade: origin=%r not trusted",
            auth.request_origin(websocket.headers),
        )
        await websocket.close(code=1008)
        return
    token = websocket.cookies.get(auth.SESSION_COOKIE)
    decoded = auth.decode_session(token) if token else None
    user_id, issued_at = decoded if decoded else (None, None)
    row = users_store.get_by_id(user_id) if user_id is not None else None
    if row is None or not row["is_active"] or auth.session_revoked(row, issued_at):
        await websocket.close(code=1008)  # policy violation
        return

    # Which workspaces this socket may hear about. Held in a mutable set that
    # the heartbeat below refreshes, so revoking a grant takes effect within
    # one heartbeat instead of lasting until the tab is closed.
    is_admin = bool(row.get("is_admin"))
    allowed: set[str] = set(users_store.accessible_account_ids(user_id))

    def visible(account_id: str) -> bool:
        # An event with no account_id belongs to no workspace; admins only.
        return is_admin or (bool(account_id) and account_id in allowed)

    await websocket.accept()
    queue = hub.subscribe(visible)
    try:
        while True:
            # Never block on the queue indefinitely. uvicorn's shutdown waits
            # for this handler to finish *before* it runs the lifespan
            # shutdown that would post hub.close()'s wake-up sentinel - so a
            # handler parked forever on `queue.get()` deadlocks the stop
            # until systemd SIGKILLs the process (confirmed live: every
            # `systemctl stop` with a dashboard tab open took 90s and ended
            # in state 'failed'). Waking on a timer and touching the socket
            # is what lets a closing connection be noticed from this side.
            try:
                event = await asyncio.wait_for(queue.get(), timeout=EVENTS_HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                # A ping the browser ignores; a closed transport raises here,
                # which is exactly the signal to leave. Also frees the queue
                # of a tab that vanished without a close frame.
                #
                # It is also the cheapest place to notice that this user has
                # been deactivated, had their sessions revoked, or lost access
                # to a workspace. `to_thread` because UserStore is blocking
                # psycopg behind a single lock, and this tick is load-bearing
                # for graceful shutdown - blocking the loop here would revive
                # the very deadlock the timeout exists to prevent.
                row = await asyncio.to_thread(users_store.get_by_id, user_id)
                if (
                    row is None
                    or not row["is_active"]
                    or auth.session_revoked(row, issued_at)
                ):
                    break
                is_admin = bool(row.get("is_admin"))
                allowed = set(
                    await asyncio.to_thread(
                        users_store.accessible_account_ids, user_id
                    )
                )
                await websocket.send_text(HEARTBEAT_FRAME)
                continue
            if event is None:  # hub.close() sentinel: server is shutting down
                break
            # Checked again on the way out: an event can be queued before a
            # permission is revoked and sent after.
            if not visible(str(event.get("account_id") or "")):
                continue
            await websocket.send_text(json.dumps(event, ensure_ascii=False))
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
        # RuntimeError: send on a connection uvicorn already closed.
        # CancelledError: uvicorn's graceful-shutdown timeout cancelling us.
        pass
    finally:
        hub.unsubscribe(queue)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    # Before binding a port: refuse to come up on a configuration that only
    # looks secure (production without a signing key, Access without an
    # audience tag). Failing here is loud; failing silently is not.
    security_config.validate_at_startup()
    # SUPERVISOR_HOST/SUPERVISOR_PORT are already the exact env vars
    # compose.yaml hands to the dashboard container as its proxy target
    # (see compose.yaml) - reading the same two here means changing one line
    # in .env moves both sides together. Before this, the container's proxy
    # target was configurable but this process's own bind port was not,
    # so setting SUPERVISOR_PORT alone silently broke the dashboard (nginx
    # proxying to a port nothing was listening on).
    host = os.environ.get("SUPERVISOR_HOST", "127.0.0.1")
    port = int(os.environ.get("SUPERVISOR_PORT", "28080"))
    # A hard ceiling on graceful shutdown. Without it uvicorn waits forever
    # for open connections (the dashboard's /api/events WebSocket never ends
    # on its own), systemd gives up after TimeoutStopSec and SIGKILLs - which
    # skips the lifespan shutdown entirely and is exactly what deploy/README.md
    # warns can leave tasks 'running' to be replayed on the next boot. The
    # heartbeat in the events handler normally ends things well inside this.
    uvicorn.run(
        app, host=host, port=port, log_level="info",
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )


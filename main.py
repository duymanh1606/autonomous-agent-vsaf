import argparse
import asyncio
import logging
from logging.handlers import RotatingFileHandler
import os
import signal
import sys
from pathlib import Path
from uuid import uuid4

from ai.client import GeminiVisionClient
from control.events import EventBus
from control.server import ControlServer
from control.settings import apply_runtime_settings
from control.state import RuntimeStatus
from core.agent import Agent
from core.state_verifier import StateVerifier
from core.graph import AgentGraph
from core.memory import Memory
from core.llm_planner import LlmPlanner
from core.policy import PolicyEngine
from core.recovery import RecoveryManager
from core.schedule import OperatingHours
from core.router import TaskRouter
from core.scheduler import Scheduler
from core.state import AgentState
from data_collection.gui_dataset_collector import GuiDatasetCollector
from grounding.dom_resolver import DomResolver
from grounding.action_executor import ActionExecutor
from grounding.fixed_coordinate_resolver import CoordinateResolver
from grounding.gemini_grounding import GeminiGrounding
from grounding.target_resolver import TargetResolver
from load_config import BASE_DIR, load_config
from perception.browser_observer import BrowserObserver
from perception.dom_observer import DomObserver
from perception.state_parser import StateParser
from perception.vision_fallback import VisionFallback
from platforms.tiktok.browsing import BrowseFeedSkill
from platforms.tiktok.inbox_backfill import InboxBackfillSkill
from platforms.tiktok.inbox_watcher import InboxScanSkill
from platforms.tiktok.messaging import BatchMessageSkill, ReplyMessageSkill
from platforms.tiktok.screens import TikTokScreen
from platforms.tiktok_web.adapter import DETERMINISTIC_ROUTES, TikTokWebAdapter
from platforms.tiktok_web.selectors import SELECTORS, TARGET_SELECTORS
from skills.registry import SkillRegistry
from storage.database import Database, SecondaryConnection, resolve_dsn
from storage.repositories import TaskRepository
from storage.conversation_context import ConversationContextRepository
from integrations.sheet_export import configured as sheet_export_configured
from storage.paths import get_account_media_dir, get_account_upload_dir
from storage.product_repository import ProductRepository
from tools.browser import ChromeBrowserTool
from tools.registry import ToolRegistry
from models.account import AccountContext
from models.auth import AuthState
from models.tasks import Task
from agent.followup_policy import FollowUpPolicy
from agent.message_analyzer import MessageAnalyzer
from agent.message_router import MessageRouter
from agent.product_lookup import ProductLookupPolicy
from agent.reply_generator import ReplyGenerator
from agent.reply_language import DEFAULT_REPLY_LANGUAGE
from agent.scenario_policy import ScenarioPolicy
from agent.reply_service import PipelineReplyProvider
from agent.reply_validator import ReplyValidator
from agent.shop_policy import ShopPolicy
from agent.state_manager import CustomerStateManager
from llm.gemini_flash_lite import GeminiFlashLiteProvider
from memory.conversation_store import PostgresConversationStore
from memory.summary_service import ConversationSummaryService
from messaging.followup_scheduler import FollowUpScheduler
from messaging.scan_scheduler import InboxScanScheduler
from messaging.runtime import MessageRuntime
from models.analysis import DEFAULT_MESSAGE_INTENTS
from models.customer_state import DEFAULT_CUSTOMER_STAGES
from services.escalation import PostgresEscalationService
from services.handoff import HumanHandoff
from storage.inbox_ledger import InboxLedgerRepository
from storage.reply_timings import ReplyTimingRepository
from services.media_cache import MediaCache
from services.metrics import MessageMetrics
from tools.registry import BusinessToolRegistry
from tools.business import register_business_services
from storage.conversation_control import ConversationControlRepository


def _resolve(value):
    path = Path(value)
    return str(path if path.is_absolute() else BASE_DIR / path)


def configure_logging(config):
    """Configure operator-visible logs without including task payloads or secrets."""
    level = getattr(
        logging, str(config.get("logging.level", "INFO")).upper(), logging.INFO
    )
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    handlers = []
    if config.get("logging.console", True):
        # stdout keeps all runtime perception traces visible in the terminal
        # that launched `uv run main.py` (warnings/errors are included).
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(formatter)
        handlers.append(console)
    if config.get("logging.file", True):
        log_path = Path(_resolve(config.get("logging.file_path", "data/logs/agent.log")))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if config.get("logging.rotation.enabled", True):
            file_handler = RotatingFileHandler(
                log_path,
                maxBytes=int(config.get("logging.rotation.max_size_mb", 20) * 1024 * 1024),
                backupCount=int(config.get("logging.rotation.backup_count", 5)),
                encoding="utf-8",
            )
        else:
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    logging.basicConfig(level=level, handlers=handlers, force=True)
    logging.getLogger("httpx").setLevel(
        getattr(
            logging,
            str(config.get("logging.http_client_level", "WARNING")).upper(),
            logging.WARNING,
        )
    )
    logging.getLogger(__name__).info(
        "Logging ready: level=%s console=%s file=%s",
        logging.getLevelName(level),
        config.get("logging.console", True),
        config.get("logging.file", True),
    )


def _build_text_client(config, section, account_id=None):
    if not config.get("ai.enabled", False) or not config.get(f"{section}.enabled", False):
        return None
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is required when the text planner or generator is enabled"
        )
    return GeminiFlashLiteProvider(
        api_key=api_key,
        model=config.get(f"{section}.model", "gemini-3.1-flash-lite"),
        base_url=config.get(
            "ai.base_url", "https://generativelanguage.googleapis.com/v1beta"
        ),
        timeout_seconds=config.get(f"{section}.timeout_seconds", 60),
        account_id=account_id,
    )


def _shop_name(config) -> str:
    """The workspace's name (its dashboard label), shown in the Google Sheet's
    "Shop" column."""
    return config.get("account.label") or config.get("account.id")


def _build_web(config, tools, database):
    base_url = config.get("platform.tiktok_web.base_url", "https://www.tiktok.com")
    browser = ChromeBrowserTool(
        cdp_url=config.get("browser.cdp_url", "http://127.0.0.1:29222"),
        screenshot_dir=_resolve(config.get("paths.screenshots", "data/screenshots")),
        base_url=base_url,
        timeout_ms=config.get("browser.timeout_ms", 15000),
        screenshot_delay_seconds=config.get("browser.screenshot_delay_seconds", 7),
        max_screenshots_per_run=config.get("browser.max_screenshots_per_run", 9),
        allowed_media_root=_resolve(config.get(
            "messaging.media.root",
            config.get("products.product_root", "data/products"),
        )),
        # Files an operator attaches in the dashboard chat (saved there by
        # dashboard_api/routes/chat_media.py) - this workspace's only.
        extra_media_roots=(get_account_upload_dir(database.account_id),),
        media_stage_timeout_ms=(
            1000 * config.get("messaging.media_stage_timeout_seconds", 60)
        ),
        allowed_media_mime_types=config.get(
            "messaging.media.allowed_mime_types", []
        ),
        max_image_size_mb=config.get("messaging.media.max_image_size_mb", 10),
        max_video_size_mb=config.get("messaging.media.max_video_size_mb", 25),
        max_dm_video_duration_seconds=config.get(
            "messaging.media.max_dm_video_duration_seconds", 60
        ),
        auto_login_enabled=config.get("auth.auto_login.enabled", True),
        auto_login_max_attempts=config.get("auth.auto_login.max_attempts_per_process", 2),
        auto_login_cooldown_seconds=config.get("auth.auto_login.cooldown_seconds", 300),
        auto_login_post_submit_wait_seconds=config.get(
            "auth.auto_login.post_submit_wait_seconds", 3
        ),
        auto_login_fedcm_settle_seconds=config.get(
            "auth.auto_login.fedcm_settle_seconds", 2
        ),
        # Present only in the YAML of an account that opted in to egw
        # (AccountRegistry.write_config); absent = no proxy, no handler.
        egw_workspace_id=config.get("egw.workspace_id", ""),
        egw_token=config.get("egw.token", ""),
    )
    # One Chrome profile belongs to one account. Labelling the browser lets the
    # runtime refuse a task whose account does not match the logged-in session.
    browser.account_id = database.account_id
    vision_enabled = config.get("vision.enabled", True)
    gemini_client = None
    if vision_enabled and config.get("vision.gemini.enabled", True):
        # The key comes from the environment only, never from YAML. A missing
        # credential disables vision here, at boot, rather than surfacing as a
        # 401 in the middle of a task.
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if api_key:
            gemini_client = GeminiVisionClient(
                config.get("vision.base_url", "https://generativelanguage.googleapis.com/v1beta"),
                api_key,
                config.get("vision.model", "gemini-3.1-flash-lite"),
                config.get("vision.timeout_seconds", 120),
                account_id=database.account_id,
            )
        else:
            logging.getLogger(__name__).info(
                "[boot] vision fallback disabled: no GEMINI_API_KEY in the environment"
            )

    common_vision = {
        "unknown_screen": TikTokScreen.UNKNOWN,
        "enabled": vision_enabled,
        "max_targets": config.get("vision.max_targets", 20),
        "reasoning_progress_seconds": config.get("logging.reasoning_progress_seconds", 10),
    }
    vision = VisionFallback(
        analyzer=gemini_client, layer_name="gemini", **common_vision
    )
    conversation_context = ConversationContextRepository(
        database,
        config.get("memory.minimum_customer_fact_confidence", 0.70),
        shop_name=_shop_name(config),
    )
    # The export is silent when unconfigured, so say once which it is.
    if sheet_export_configured():
        logging.getLogger(__name__).info("[sheet] Google Sheet export on")
    else:
        logging.getLogger(__name__).warning(
            "[sheet] Google Sheet export off: GSHEET_WEBHOOK_URL is not set"
        )
    # DOM state is read in one place and shared by the observer, the flow
    # verifier and the platform adapter.
    dom_observer = DomObserver(
        browser,
        StateParser(),
        default_timeout_ms=config.get("perception.dom_state_timeout_ms", 4000),
        poll_interval_ms=config.get("perception.dom_poll_interval_ms", 150),
    )
    observer = BrowserObserver(
        browser, vision,
        confidence_threshold=config.get("vision.screen_confidence_threshold", 0.80),
        target_confidence_threshold=config.get("vision.target_confidence_threshold", 0.85),
        max_vision_capture_attempts=config.get("vision.max_capture_attempts", 3),
        vision_retry_delay_seconds=config.get("vision.retry_delay_seconds", 2),
        extension_event_wait_ms=config.get("browser.extension_event_wait_ms", 1200),
        conversation_context_repository=conversation_context,
        dom_observer=dom_observer,
    )
    dom_resolver = DomResolver(browser, SELECTORS, TARGET_SELECTORS)
    action_executor = ActionExecutor(browser)
    platform = TikTokWebAdapter(
        browser, observer, base_url,
        dom_resolver=dom_resolver,
        action_executor=action_executor,
        dom_observer=dom_observer,
    )
    # The verifier needs the adapter's action semantics and the adapter needs
    # the verifier's DOM gate, so the link is completed after construction.
    state_verifier = StateVerifier(
        platform,
        dom_observer,
        timeout_ms=config.get("perception.state_verify_timeout_ms", 4000),
    )
    platform.state_verifier = state_verifier
    for name, handler in {
        "browser.open_tiktok": browser.open_tiktok,
        "browser.check_auth": browser.check_auth,
        "browser.auto_login": browser.auto_login,
        "browser.close": browser.close,
        "browser.back": browser.back,
        "browser.screenshot": browser.screenshot,
        "browser.snapshot": browser.snapshot,
        "browser.click_bbox": browser.click_bbox,
    }.items():
        tools.register(name, handler)
    platform.register_tools(tools)
    recovery_actions = ("browser.snapshot", "browser.back", "browser.screenshot")
    return browser, observer, platform, recovery_actions, state_verifier


def _build_media_cache(config, browser, account_id):
    """The on-disk picture cache the dashboard displays from, or `None`.

    Off by config, the rows keep only TikTok's own `src` - which the dashboard
    can embed until it expires, and not at all for the DM-private host. See
    services/media_cache.py for why a local copy is what makes Inbox pictures
    actually work.
    """
    if not config.get("messaging.media_cache.enabled", True):
        logging.getLogger(__name__).info(
            "[MEDIA] cache disabled by config; Inbox shows CDN URLs only"
        )
        return None
    return MediaCache(
        browser.fetch_media_bytes,
        get_account_media_dir(account_id),
        max_file_bytes=int(
            1024 * 1024 * config.get("messaging.media_cache.max_file_mb", 10)
        ),
        max_total_bytes=int(
            1024 * 1024 * config.get("messaging.media_cache.max_total_mb", 512)
        ),
    )


def _build_message_runtime(
    config, database, repository, platform_name,
    business_services=None, strong_llm=None, conversation_control=None,
    state=None, media_cache=None,
):
    if not config.get("messaging.enabled", False):
        return None, None, None, None, None
    if platform_name != "tiktok_web":
        raise ValueError("Automatic message event ingestion currently requires tiktok_web")

    api_key = os.environ.get("GEMINI_API_KEY") or config.get("llm.api_key", "")
    llm = GeminiFlashLiteProvider(
        api_key=api_key,
        model=config.get("llm.model", "gemini-3.1-flash-lite"),
        base_url=config.get(
            "llm.base_url", "https://generativelanguage.googleapis.com/v1beta"
        ),
        temperature=config.get("llm.temperature", 0.2),
        max_output_tokens=config.get("llm.max_output_tokens", 2048),
        timeout_seconds=config.get("llm.timeout_seconds", 30),
        max_retries=config.get("llm.max_retries", 2),
        retry_backoff_seconds=config.get("llm.retry_backoff_seconds", 1),
        account_id=database.account_id,
    )
    intents = config.get("routing.registered_intents", list(DEFAULT_MESSAGE_INTENTS))
    stages = config.get("routing.registered_stages", list(DEFAULT_CUSTOMER_STAGES))
    conversations = PostgresConversationStore(database, shop_name=_shop_name(config))
    state_manager = CustomerStateManager(stages)
    business_tools = BusinessToolRegistry(
        config.get("messaging.business_tools.allowed", [])
    )
    services = dict(business_services or {})
    # Without a product service every stock/price question routes to a human,
    # because `ReplyValidator` refuses to answer from the model's own knowledge
    # when the analysis said a tool was required. The catalogue is local data,
    # so wiring it in costs nothing and is what makes those answers possible.
    if services.get("product") is None and config.get("products.enabled", True):
        product_repository = ProductRepository(
            config.get("products.product_root", "data/products")
        )
        services["product"] = product_repository
        # The same verified catalogue owns total/variant stock, so both tools
        # use one cached view of each product.json.
        services.setdefault("inventory", product_repository)
    register_business_services(business_tools, **services)
    metrics = MessageMetrics()
    analyzer = MessageAnalyzer(llm, intents, stages, business_tools.names)
    router = MessageRouter(
        config.get("routing.strong_llm_threshold", 0.65),
        config.get("routing.human_intents", []),
    )
    # `products.lookup_enabled: false` answers product questions from the
    # conversation context instead of querying the catalogue.
    product_lookup = ProductLookupPolicy(
        config.get("products.lookup_enabled", True),
        # A question naming no product is about the shop's default one.
        catalogue=services.get("product"),
        default_product_id=config.get("products.default_product_id"),
    )
    # Operator-authored Khách/Agent example exchanges, edited from the dashboard
    # (control/settings.py's "messaging.scenario_examples" setter) with no
    # restart needed - shared so the edit reaches both generators below at once.
    scenario_policy = ScenarioPolicy(config.get("messaging.scenario_examples", []))
    # None (unset): each generator uses its reply language's default rules.
    reply_style = config.get("messaging.reply_style")
    # Every reply is written in this language whatever the customer wrote in;
    # both generators start from the same value (control/settings.py's
    # "messaging.reply_language" setter then keeps them in step).
    reply_language = config.get("messaging.reply_language", DEFAULT_REPLY_LANGUAGE)
    reply_generator = ReplyGenerator(
        llm, config.get("messaging.max_reply_characters", 1000), product_lookup,
        scenario_policy=scenario_policy, reply_style=reply_style,
        reply_language=reply_language,
    )
    reply_validator = ReplyValidator(
        config.get("messaging.max_reply_characters", 1000)
    )
    strong_reply_generator = (
        ReplyGenerator(
            strong_llm,
            config.get("messaging.max_reply_characters", 1000),
            product_lookup,
            scenario_policy=scenario_policy, reply_style=reply_style,
            reply_language=reply_language,
        )
        if strong_llm else None
    )
    # The one analyse -> route -> run tool -> generate -> validate chain. The
    # inbox scan calls it directly to finish a conversation in place; it is
    # the only way a customer is answered.
    reply_provider = PipelineReplyProvider(
        analyzer=analyzer,
        router=router,
        state_manager=state_manager,
        reply_generator=reply_generator,
        reply_validator=reply_validator,
        business_tools=business_tools,
        strong_reply_generator=strong_reply_generator,
        product_lookup=product_lookup,
        conversation_store=conversations,
        summary_service=ConversationSummaryService(
            llm,
            config.get("memory.summary.summarize_after_messages", 20),
            config.get("memory.summary.source_message_limit", 40),
        ),
        recent_message_limit=config.get("memory.recent_message_limit", 12),
        history_message_limit=config.get("memory.history_message_limit", 100),
        unknown_intent_limit=config.get("routing.unknown_intent_limit", 2),
        metrics=metrics,
        enabled_media_kinds=("image", "video"),
        media_cache=media_cache,
        # Lets a conversation notice a product it discussed has been deleted.
        catalogue=services.get("product"),
        unsupported_message_reply=config.get("messaging.unsupported_message_reply"),
        # Delivery facts every reply may quote (agent/shop_policy.py).
        shop_policy=ShopPolicy(config.get("products.shipping")),
        order_handoff=config.get("messaging.order_handoff", True),
    )
    # One way to answer a customer: the inbox scan, which calls
    # `reply_provider` in place and settles each message in the ledger (see
    # messaging/runtime.py for the queue-driven path that was removed).
    inbox_watcher = None
    if config.get("messaging.auto_check.enabled", True):
        inbox_watcher = InboxScanScheduler(
            repository,
            interval_seconds=config.get("messaging.auto_check.interval_seconds", 10),
            platform=platform_name,
            completed_task_retention=config.get(
                "messaging.auto_check.completed_task_retention", 100
            ),
            schedule=OperatingHours.from_config(config),
            state=state,
        )
    # Operator-authored "silence for N minutes -> send this" rules, edited
    # from the dashboard (control/settings.py's "messaging.follow_up.rules"
    # setter) with no restart needed - the policy is shared with the
    # scheduler so an edit changes what the very next scan enqueues.
    followup_policy = FollowUpPolicy(config.get("messaging.follow_up.rules", []))
    followup_scheduler = None
    if config.get("messaging.follow_up.enabled", True):
        followup_scheduler = FollowUpScheduler(
            database, repository, followup_policy,
            platform=platform_name,
            interval_seconds=config.get("messaging.follow_up.interval_seconds", 60),
            schedule=OperatingHours.from_config(config),
            state=state,
            # Live: a dashboard language switch reaches the next scan tick.
            reply_language=lambda: reply_provider.reply_language,
        )
    return MessageRuntime(
        inbox_watcher=inbox_watcher,
        followup_scheduler=followup_scheduler,
    ), llm, reply_provider, followup_policy, scenario_policy


def build_agent(config_path=None, *, business_services=None, strong_llm=None):
    config = load_config(config_path)
    # The account is resolved before anything else: the database, every
    # repository and every task below are scoped by it.
    account = AccountContext.from_config(config)
    logging.getLogger(__name__).info(
        "[boot] account=%s platform=%s runtime=langgraph",
        account.account_id, account.platform,
    )
    database = Database(
        resolve_dsn(config),
        BASE_DIR / "storage" / "schema.sql",
        account_id=account.account_id,
    )
    database.register_account(
        platform=account.platform,
        username=account.username,
        display_name=account.display_name,
    )
    # Repositories take their scope from the database they were opened for,
    # so none of the call sites below can silently drop the account filter.
    repository = TaskRepository(
        database,
        task_lease_seconds=config.get("messaging.task_lease_seconds", 180),
        send_lease_seconds=config.get("messaging.send_lease_seconds", 180),
    )
    # Manage control mode for each conversation
    conversation_control = ConversationControlRepository(
        database,
        human_takeover_ttl_minutes=config.get(
            "conversation_control.human_takeover_ttl_minutes", 60
        ),
        waiting_human_ttl_minutes=config.get(
            "conversation_control.waiting_human_ttl_minutes", 20
        ),
    )

    recovered_tasks = repository.requeue_interrupted_tasks()
    expired_sends = repository.expire_interrupted_send_tasks()
    if expired_sends:
        logging.getLogger(__name__).warning(
            "Expired %s interrupted send task(s); the open conversation decides "
            "whether they are retried",
            expired_sends,
        )
    if recovered_tasks:
        logging.getLogger(__name__).warning(
            "Requeued %s interrupted task(s) for LangGraph checkpoint resume",
            recovered_tasks,
        )
    tools = ToolRegistry()
    platform_name = config.get("platform.default", "tiktok_web")
    if platform_name != "tiktok_web":
        database.close()
        raise ValueError(f"Unsupported platform: {platform_name}")
    browser, observer, platform, recovery_actions, state_verifier = _build_web(
        config, tools, database
    )
    input_action = "browser.input_text"

    configured_allowed = set(config.get("tools.allowed", []))
    runtime_actions = tuple(name for name in tools.names if name in configured_allowed)
    low_level_actions = {
        "browser.input_text", "browser.close", "browser.check_auth",
        "platform.detect_unread",
        "platform.open_conversation", "platform.open_unread_conversation",
        "platform.focus_message_input", "platform.send_message",
        "platform.attach_message_media", "platform.clear_message_draft",
        "platform.retry_send", "platform.list_conversations",
        "platform.list_unread_conversations", "platform.list_all_conversations",
        "platform.reload_page",
        "platform.detect_message_requests", "platform.open_message_requests",
        "platform.list_message_requests", "platform.open_message_request",
        "platform.accept_message_request", "platform.return_to_primary_inbox",
    }
    planner_actions = tuple(name for name in runtime_actions if name not in low_level_actions)

    # The planner's LLM only runs for a capability with no skill and no
    # deterministic route, and it is shown the whole observation - customer
    # messages included - before it picks a UI action. That is a customer
    # able to steer what gets clicked, so it is off unless a deployment asks
    # for it by name; without a client the planner aborts such a task.
    planner_client = (
        _build_text_client(config, "ai.planner", account_id=database.account_id)
        if config.get("ai.planner.llm_fallback", False) else None
    )
    skills = SkillRegistry()
    reply_skill = ReplyMessageSkill(
        input_action=input_action,
        max_attachments=config.get("messaging.media.max_attachments", 2),
    )
    skills.register(reply_skill)
    skills.register(BatchMessageSkill())
    skills.register(BrowseFeedSkill())
    inbox_skill = None
    inbox_backfill_skill = None
    # Everything the scan cannot answer goes through here: an escalation row so
    # the case survives a restart, and an event so the dashboard raises a
    # notification. The event bus is attached later, once the control server
    # exists; until then the handoff still records and logs.
    handoff = HumanHandoff(
        account_id=account.account_id,
        escalation_service=PostgresEscalationService(database),
        platform=platform_name,
        conversation_control=conversation_control,
    )
    if platform_name == "tiktok_web" and config.get("messaging.enabled", False):
        # `reply_provider` is attached once the message runtime exists, so the
        # scan can hand one conversation straight through to send+verify.
        inbox_skill = InboxScanSkill(
            config.get("messaging.auto_check.max_conversations_per_scan", 5),
            reply_skill=reply_skill,
            reply_quiet_period_seconds=config.get(
                "messaging.auto_check.reply_quiet_period_seconds", 20
            ),
            reply_wait_timeout_seconds=config.get(
                "messaging.auto_check.max_burst_wait_seconds",
                config.get("messaging.auto_check.reply_wait_timeout_seconds", 45),
            ),
            reply_wait_poll_seconds=config.get(
                "messaging.auto_check.reply_wait_poll_seconds", 2
            ),
            auto_accept_message_requests=config.get(
                "messaging.auto_check.auto_accept_message_requests", False
            ),
            request_slot_reservation=config.get(
                "messaging.auto_check.request_slot_reservation", 1
            ),
            trust_negative_unread_badge=config.get(
                "messaging.auto_check.trust_negative_unread_badge", False
            ),
            message_request_retry_prompt=config.get(
                "messaging.auto_check.message_request_retry_prompt",
                "Quý khách vui lòng gửi lại yêu cầu giúp shop ạ.",
            ),
            sync_read_elsewhere=config.get(
                "messaging.auto_check.sync_read_elsewhere", True
            ),
            sync_rows=config.get("messaging.auto_check.sync_rows", 10),
            sync_per_scan=config.get("messaging.auto_check.sync_per_scan", 1),
            sync_cooldown_seconds=config.get(
                "messaging.auto_check.sync_cooldown_seconds", 300
            ),
            handoff=handoff,
            # Which customers are still waiting, and which messages were
            # already answered - kept in PostgreSQL so a restart does not lose
            # the threads whose unread marker the agent itself cleared.
            ledger=InboxLedgerRepository(database),
            # How long each step of every reply took ([TIMING] log + table).
            timings=ReplyTimingRepository(database),
            conversation_control=conversation_control,
        )
        skills.register(inbox_skill)
        inbox_backfill_skill = InboxBackfillSkill(
            config.get("messaging.inbox_backfill.max_conversations", 300),
            config.get("messaging.inbox_backfill.batch_size", 20),
            # Shares the same ledger table as inbox_skill, under its own
            # reserved keys - a fresh InboxLedgerRepository is a stateless
            # wrapper, not a second copy of the data.
            ledger=InboxLedgerRepository(database),
        )
        skills.register(inbox_backfill_skill)
    limits = config.get("policy.limits", {})
    policy = PolicyEngine(
        allowed_tools=runtime_actions,
        blocked_tools=config.get("policy.blocked_tools", []),
        max_actions_per_task=limits.get("max_actions_per_task", 100),
        max_retries_per_action=limits.get("max_retries_per_action", 3),
        max_task_runtime_seconds=limits.get("max_task_runtime_seconds", 300),
        interaction_limits=config.get("policy.interaction_limits"),
        visual_target_confidence_threshold=config.get(
            "vision.target_confidence_threshold", 0.85
        ),
        send_repository=repository,
        conversation_control=conversation_control,
    )
    temporary_backoff = tuple(config.get("auth.temporary_backoff_seconds", [2, 5, 15]))
    recovery = RecoveryManager(
        limits.get("max_retries_per_action", 3),
        reobserve_action=recovery_actions[0], back_action=recovery_actions[1],
        screenshot_action=recovery_actions[2],
        temporary_backoff_seconds=temporary_backoff,
    )
    # Fixed coordinates come from one shared file measured on one screen at one
    # window size. A second account whose Chrome window differs would click the
    # wrong place without any error, so extra accounts fall back to DOM and
    # vision resolution instead of inheriting coordinates that do not fit.
    if config.get("grounding.fixed_coordinates_enabled", True):
        fixed_resolver = CoordinateResolver.from_file(
            confidence_threshold=config.get(
                "vision.screen_confidence_threshold", 0.80
            )
        )
    else:
        # An empty resolver rather than `None`: `core/graph.py` calls
        # `target_resolver.fixed` in six places without a None check, so an
        # empty one keeps every caller working while resolving nothing.
        fixed_resolver = CoordinateResolver({
            # A viewport is required by the constructor; with no coordinates
            # defined nothing is ever resolved against it.
            "base_viewport": {"width": 1920, "height": 1080},
            "coordinates": {}, "actions": {},
        })
        logging.info(
            "Fixed coordinates disabled for this account; DOM and vision only"
        )
    target_resolver = TargetResolver(
        platform.dom_resolver, fixed_resolver, GeminiGrounding(observer)
    )
    dataset_collector = GuiDatasetCollector(
        root=_resolve(config.get("gui_training.directory", "data/gui_training")),
        enabled=config.get("gui_training.enabled", True),
    )
    graph = AgentGraph(
        Scheduler(repository), observer, TaskRouter(skills),
        LlmPlanner(
            planner_client, tools, platform, config.get("ai.planner.temperature", 0.2),
            available_actions=planner_actions,
            reasoning_progress_seconds=config.get("logging.reasoning_progress_seconds", 10),
        ),
        policy, recovery, tools, platform,
        Memory(repository, config.get("memory.action_history.max_entries_per_task", 500)),
        AgentState(auth_enforced=config.get("auth.require_logged_in", True)),
        config.get("scheduler.idle.sleep_seconds", 3),
        auth_poll_seconds=config.get("auth.manual_login_poll_seconds", 5),
        auth_rate_limit_poll_seconds=config.get("auth.rate_limited_poll_seconds", 60),
        temporary_backoff_seconds=temporary_backoff,
        checkpoint_dsn=resolve_dsn(config),
        recursion_limit=config.get("langgraph.recursion_limit", 1000),
        target_resolver=target_resolver,
        dataset_collector=dataset_collector,
        action_verifier=state_verifier,
        account=account,
    )
    graph.browser_account_id = getattr(browser, "account_id", None)
    graph.schedule = OperatingHours.from_config(config)
    agent = Agent(graph)
    agent.database = database
    agent.browser = browser
    agent.config = config
    agent.repository = repository

    # Conversation-level human takeover
    agent.conversation_control = conversation_control
    agent.inbox_skill = inbox_skill
    agent.inbox_backfill_skill = inbox_backfill_skill
    # `main()` attaches the event bus to it once the control server is up.
    agent.handoff = handoff
    (
        agent.message_runtime, agent.message_llm, reply_provider, followup_policy,
        scenario_policy,
    ) = _build_message_runtime(
        config, database, repository, platform_name,
        business_services=business_services, strong_llm=strong_llm,
        conversation_control=conversation_control,
        state=agent.state,
        media_cache=_build_media_cache(config, browser, database.account_id),
    )
    # Reached by control/settings.py's "messaging.scenario_examples" setter.
    agent.scenario_policy = scenario_policy
    # Reached by control/settings.py's "messaging.follow_up.rules" setter.
    # None when messaging is disabled for this runtime (see the early return
    # at the top of `_build_message_runtime`) - the setter already reports
    # that cleanly rather than crashing.
    agent.followup_policy = followup_policy
    if inbox_skill is not None:
        inbox_skill.reply_provider = reply_provider
    if inbox_backfill_skill is not None:
        inbox_backfill_skill.reply_provider = reply_provider
    return agent


async def _seed_inbox_backfill(agent):
    """Queue the one-time historical inbox import exactly once per account.

    `InboxBackfillSkill.execute` also checks the ledger and no-ops when this
    already ran; the checks here only stop the same one-shot task from being
    queued again on every restart before the skill gets a chance to run.
    """
    if await agent.inbox_backfill_skill.already_done():
        return
    if await agent.repository.has_active_capability("messaging.backfill_inbox"):
        return
    await agent.repository.submit(Task(
        id=f"inbox-backfill-{uuid4()}",
        account_id=agent.database.account_id,
        capability="messaging.backfill_inbox",
        platform="tiktok_web",
        priority=4,
        payload={"automatic": True},
    ))
    logging.info("[boot] queued the one-time historical inbox import")


async def main(config_path=None):
    config = load_config(config_path)
    configure_logging(config)
    agent = build_agent(config_path)
    stop = _install_signal_handlers(agent)
    control = _build_control_server(agent, config, stop)
    if control is not None:
        await control.start()
        agent.events = control.events
        # From here on, a conversation handed to a human also reaches the
        # dashboard instead of only the log.
        if getattr(agent, "handoff", None) is not None:
            agent.handoff.events = control.events
        if getattr(agent, "inbox_skill", None) is not None:
            agent.inbox_skill.events = control.events
        # Same wiring for CAPTCHA/logout: AgentGraph.events was None until
        # now (see core/graph.py._notify_auth), so a workspace hitting a
        # security challenge or losing its session during boot, before the
        # control server was up, simply skips that one notification instead
        # of erroring - the next poll cycle (still blocked) announces it.
        agent.graph.events = control.events
    try:
        if agent.browser:
            logging.info("Startup: connecting to Dedicated Chrome through CDP")
            try:
                connection = await agent.browser.start()
                if not connection.get("tiktok_attached", False):
                    logging.error(
                        "Startup stopped: open TikTok manually in Dedicated Chrome; "
                        "URL navigation and reload are disabled"
                    )
                    return
                if agent.browser.egw_enabled:
                    # Fail-closed, same posture as egw itself: a workspace
                    # whose traffic cannot be shown to leave through its own
                    # IP must not touch a customer conversation.
                    egress = await agent.browser.verify_egress_ip(
                        config.get("egw.expected_ip", "")
                    )
                    if not egress.get("verified"):
                        logging.error(
                            "Startup stopped: egress IP not verified for workspace %s "
                            "(reason=%s observed=%s). Check egw route health and the "
                            "account's egw token before starting this workspace again.",
                            agent.browser.egw_workspace_id,
                            egress.get("reason"), egress.get("observed_ip"),
                        )
                        return
                logging.info("Startup: CDP connected; checking TikTok authentication")
                auth = await agent.browser.check_auth()
            except Exception as exc:
                logging.error("Chrome CDP preflight failed: %s", exc, exc_info=True)
                return
            auth_state = AuthState(auth.get("auth_state", AuthState.UNKNOWN.value))
            agent.graph.state.auth_state = auth_state
            if auth_state != AuthState.LOGGED_IN:
                authenticated = await _wait_for_authentication(agent, stop, config)
                if not authenticated:
                    logging.info("Authentication wait stopped during shutdown")
                    return
            agent.graph.state.confirm_logged_in()
            logging.info("Chrome CDP connected and TikTok auth confirmed: LOGGED_IN")
            if getattr(agent, "inbox_backfill_skill", None) is not None:
                await _seed_inbox_backfill(agent)
        if agent.message_llm:
            try:
                await agent.message_llm.validate_model()
            except Exception as exc:
                logging.error("Gemini message model preflight failed: %s", exc, exc_info=True)
                return
            logging.info(
                "Gemini message model preflight passed: model=%s",
                agent.message_llm.model_name,
            )
        if agent.message_runtime:
            await agent.message_runtime.start()
            logging.info("Customer message pipeline started")
        logging.info("LangGraph runtime started; waiting for tasks")
        await agent.run()
    except asyncio.CancelledError:
        logging.info("Shutdown requested by operator")
    finally:
        logging.info("Agent is shutting down")
        if control is not None:
            await control.stop()
        if getattr(agent, "message_runtime", None):
            await agent.message_runtime.stop()
        if getattr(agent, "message_llm", None):
            await agent.message_llm.close()
        if agent.browser:
            await agent.browser.close()
        await agent.graph.close()
        agent.database.close()
        # The control server's own connection (see _control_conversation_control).
        side = getattr(getattr(control, "conversation_control", None), "database", None)
        if side is not None and hasattr(side, "close") and side is not agent.database:
            side.close()


async def _wait_for_authentication(agent, stop, config):
    """Keep the process and control plane alive while login is recovered."""
    last_status = None
    normal_poll = max(1.0, float(config.get("auth.manual_login_poll_seconds", 5)))
    challenge_poll = max(
        normal_poll, float(config.get("auth.rate_limited_poll_seconds", 60))
    )
    while agent.state.running and not stop.is_set():
        result = await agent.browser.auto_login()
        auth_state = AuthState(result.get("auth_state", AuthState.UNKNOWN.value))
        agent.graph.state.auth_state = auth_state
        if auth_state == AuthState.LOGGED_IN:
            return True

        status = (auth_state.value, result.get("reason"))
        if status != last_status:
            logging.warning(
                "Authentication pending: state=%s auto_login=%s",
                auth_state.value, result.get("reason", "not_attempted"),
            )
            last_status = status
        delay = challenge_poll if auth_state in {
            AuthState.AUTH_RATE_LIMITED, AuthState.SECURITY_CHALLENGE,
        } else normal_poll
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
    return False


def _submittable_capabilities(agent, config):
    """What an outside caller may submit, or None for anything.

    A capability no skill claims and no deterministic route covers falls
    through to the planner's LLM. With that fallback off (the default) such a
    task could only be aborted, so it is refused at the door instead - and the
    door is where a caller learns it made a typo, rather than in a failed task.
    """
    if config.get("ai.planner.llm_fallback", False):
        return None
    allowed = set(DETERMINISTIC_ROUTES)
    for skill in agent.graph.router.skills.skills:
        allowed.update(skill.capabilities)
    return frozenset(allowed)


def _build_control_server(agent, config, stop):
    """Expose this runtime to the dashboard supervisor. Optional by design."""
    if not config.get("control.enabled", True):
        return None

    account_id = AccountContext.from_config(config).account_id
    events = EventBus(account_id)
    status = RuntimeStatus(
        account_id=account_id,
        runtime="langgraph",
        agent=agent,
        health_probe=lambda: _health_probe(agent),
    )

    allowed_capabilities = _submittable_capabilities(agent, config)

    async def submit(*, capability, payload, priority, platform, task_id=None):
        if allowed_capabilities is not None and capability not in allowed_capabilities:
            raise ValueError(
                f"unknown capability {capability!r}: it has no skill and no "
                "deterministic route, and the planner's LLM fallback is off"
            )
        resolved_id = task_id or f"ui-{uuid4()}"
        # A caller-supplied id (the dashboard's manual-send flow) makes this
        # idempotent: a retried POST with the same id must not create a
        # second task, so an existing row is left untouched instead of
        # re-inserted (TaskRepository.submit has no ON CONFLICT clause).
        existing = agent.database.conn.execute(
            "SELECT id FROM tasks WHERE account_id = %s AND id = %s",
            (account_id, resolved_id),
        ).fetchone()
        if existing is None:
            task = Task(
                id=resolved_id,
                account_id=account_id,
                capability=capability,
                platform=platform,
                priority=priority,
                payload=payload,
            )
            await agent.repository.submit(task)
            # The loop polls the queue on an idle timer; this ends that wait
            # now. Only worth doing for a row that was actually inserted - a
            # retried POST for an existing task has nothing new to claim.
            notify = getattr(agent.graph, "notify_task_submitted", None)
            if notify is not None:
                notify()
        return resolved_id

    async def apply_settings(values):
        return apply_runtime_settings(agent, config, values)

    def request_shutdown():
        # `stop` alone only wakes a waiter; the graph loop exits on `running`.
        agent.state.running = False
        stop.set()

    return ControlServer(
        status, events,
        host=config.get("control.host", "127.0.0.1"),
        port=config.get("control.port", 8900),
        task_submitter=submit,
        settings_applier=apply_settings,
        shutdown=request_shutdown,
        conversation_control=_control_conversation_control(agent, config),
    )


def _control_conversation_control(agent, config):
    """Takeover/release for the control server, on a connection of its own.

    The control server runs on its own thread (see control/server.py), so it
    must not borrow the agent's database handle: two threads interleaving
    statements inside one transaction is how an operator's takeover would end
    up committed as part of whatever the agent happened to be doing. Its own
    connection is also what makes the split worth having - a takeover is two
    indexed queries and now waits for nothing the agent is busy with.

    Same repository class and the same TTLs as the agent's own instance, so
    both processes judge an expired claim identically.
    """
    if getattr(agent, "conversation_control", None) is None:
        return None
    database = getattr(agent, "database", None)
    account_id = getattr(database, "account_id", None)
    if not account_id:
        return None
    return ConversationControlRepository(
        SecondaryConnection(account_id=account_id, config=config),
        account_id=account_id,
        human_takeover_ttl_minutes=config.get(
            "conversation_control.human_takeover_ttl_minutes", 60
        ),
        waiting_human_ttl_minutes=config.get(
            "conversation_control.waiting_human_ttl_minutes", 20
        ),
    )


async def _health_probe(agent):
    """Six signals that already exist in the runtime but were never exposed."""
    checks = {}

    database = getattr(agent, "database", None)
    try:
        database.conn.execute("SELECT 1").fetchone()
        checks["database"] = {"ok": True, "detail": str(database.path)}
    except Exception as exc:  # noqa: BLE001 - reported in the health payload
        checks["database"] = {"ok": False, "detail": type(exc).__name__}

    browser = getattr(agent, "browser", None)
    if browser is None:
        checks["chrome"] = {"ok": False, "detail": "no browser tool"}
    else:
        try:
            alive = browser.is_alive()
            checks["chrome"] = {"ok": bool(alive), "detail": "cdp attached" if alive else "not connected"}
        except Exception as exc:  # noqa: BLE001 - reported in the health payload
            checks["chrome"] = {"ok": False, "detail": type(exc).__name__}
        checks["extension"] = {
            "ok": bool(getattr(browser, "_extension_connected", False)),
            "detail": "bridge reporting DOM state",
        }

    state = getattr(agent, "state", None)
    auth = getattr(getattr(state, "auth_state", None), "value", None)
    checks["auth"] = {"ok": auth == "LOGGED_IN", "detail": auth or "UNKNOWN"}

    llm = getattr(agent, "message_llm", None)
    checks["llm"] = (
        {"ok": True, "detail": llm.model_name} if llm is not None
        else {"ok": False, "detail": "not configured"}
    )

    checks["agent"] = {
        "ok": bool(getattr(state, "running", False)),
        "detail": "paused" if getattr(state, "paused", False) else "running",
    }
    return checks


def _install_signal_handlers(agent):
    """Ask the graph loop to stop at the next task boundary, then hard-stop.

    Without this SIGTERM kills the process mid-task and leaves `tasks.status`
    stuck at `running`, which the next boot silently requeues. A supervisor
    stopping an agent uses SIGTERM, so this is what makes a clean stop possible.
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop():
        if stop.is_set():
            return
        stop.set()
        agent.state.running = False
        logging.info("Shutdown requested; finishing the current task first")

    for name in ("SIGINT", "SIGTERM"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            loop.add_signal_handler(number, request_stop)
        except NotImplementedError:
            pass
    return stop


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Run the LangGraph agent runtime for one account."
    )
    parser.add_argument(
        "--config",
        dest="config_path",
        default=None,
        help="Path to the account's config YAML (default: configs/config.yaml)",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    arguments = _parse_args(sys.argv[1:])
    asyncio.run(main(arguments.config_path))

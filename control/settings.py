"""Which settings can change while the agent runs, and which cannot.

Config values are read once at boot and baked into constructor arguments, so
writing YAML alone changes nothing until a restart. The values below are the
exception: they live on as plain instance attributes that the running code reads
every iteration, so assigning to them takes effect immediately.

Everything not listed here is reported as `restart_required` rather than
silently accepted - a settings page that appears to work but does not is worse
than one that tells the operator to restart.
"""

import logging
from typing import Any

from agent.reply_language import REPLY_LANGUAGES, normalize_language


logger = logging.getLogger(__name__)


def _inbox_setter(attribute, minimum=0.0):
    def apply(agent, value):
        skill = getattr(agent, "inbox_skill", None)
        if skill is None:
            return False, "inbox scanning is not enabled in this runtime"
        setattr(skill, attribute, max(minimum, float(value)))
        return True, None
    return apply


def _inbox_int_setter(attribute, minimum=1, maximum=20):
    def apply(agent, value):
        skill = getattr(agent, "inbox_skill", None)
        if skill is None:
            return False, "inbox scanning is not enabled in this runtime"
        setattr(skill, attribute, max(minimum, min(maximum, int(value))))
        return True, None
    return apply


def _scenario_setter(agent, value):
    """Operator-authored Khách/Agent example exchanges (a list of
    ``{"customer": ..., "agent": ...}`` dicts).

    Shared by both `ReplyGenerator` instances (see main.py's `scenario_policy`),
    so this one assignment changes the prompt every reply uses next.
    """
    policy = getattr(agent, "scenario_policy", None)
    if policy is None:
        return False, "this runtime has no scenario policy"
    if not isinstance(value, list):
        return False, "expected a list of {customer, agent} examples"
    policy.set(value)
    return True, None


def _generators(agent):
    """Both reply generators (weak and strong LLM), via the reply provider."""
    provider = getattr(getattr(agent, "inbox_skill", None), "reply_provider", None)
    return [
        generator
        for generator in (
            getattr(provider, "reply_generator", None),
            getattr(provider, "strong_reply_generator", None),
        )
        if generator is not None
    ]


def _reply_style_setter(agent, value):
    """`messaging.reply_style`: a list of tone/layout rules, one per line, or
    None for the reply language's default rules.

    Both generators (weak and strong LLM) hang off the reply provider; each
    renders `style_rules` on every prompt, so reassigning it is what changes
    the next one.
    """
    if value is not None and not isinstance(value, list):
        return False, "expected a list of style rules"
    generators = _generators(agent)
    if not generators:
        return False, "this runtime has no reply generator"
    for generator in generators:
        generator.style_rules = None if value is None else [str(rule) for rule in value]
    return True, None


def _reply_language_setter(agent, value):
    """`messaging.reply_language`: the language every reply is written in.

    Rejected rather than silently defaulted when the code is unknown - an
    operator who picks a language the agent cannot write must see the refusal,
    not a workspace that quietly keeps answering in the old one.
    """
    code = str(value or "").strip().lower()
    if code not in REPLY_LANGUAGES:
        return False, f"unsupported language; expected one of {', '.join(REPLY_LANGUAGES)}"
    generators = _generators(agent)
    if not generators:
        return False, "this runtime has no reply generator"
    for generator in generators:
        generator.reply_language = normalize_language(code)
    return True, None


def _followup_setter(agent, value):
    """Operator-authored "silent for N minutes -> send this" rules (a list of
    ``{"id", "delay_minutes", "message", "enabled"}`` dicts).

    `FollowUpScheduler` (messaging/followup_scheduler.py) reads this same
    shared policy on its next scan tick, so an edit here needs no restart.
    """
    policy = getattr(agent, "followup_policy", None)
    if policy is None:
        return False, "this runtime has no follow-up policy"
    if not isinstance(value, list):
        return False, "expected a list of {delay_minutes, message} rules"
    policy.set(value)
    return True, None


def _shipping_setter(agent, value):
    """`products.shipping`: the shop's delivery facts (agent/shop_policy.py).

    One ShopPolicy shared with the reply provider, so the next reply quotes
    the new fee. None clears it.
    """
    provider = getattr(getattr(agent, "inbox_skill", None), "reply_provider", None)
    policy = getattr(provider, "shop_policy", None)
    if policy is None:
        return False, "this runtime has no shop policy"
    if value is not None and not isinstance(value, dict):
        return False, "expected {fee, free_from_quantity, delivery_days, cash_on_delivery, area}"
    policy.set_shipping(value)
    return True, None


def _default_product_setter(agent, value):
    """`products.default_product_id`: the product a question naming none is
    about (agent/product_lookup.py). None = the shop's only single item."""
    provider = getattr(getattr(agent, "inbox_skill", None), "reply_provider", None)
    policy = getattr(provider, "product_lookup", None)
    if policy is None:
        return False, "this runtime has no product lookup policy"
    if value is not None and not isinstance(value, str):
        return False, "expected a product id or null"
    policy.default_product_id = (value or "").strip() or None
    return True, None


def _product_lookup_setter(agent, value):
    """`products.lookup_enabled` lives on one shared policy object.

    The reply provider and both reply generators hold the same instance, so
    assigning to it switches the routing and the prompt together.
    """
    provider = getattr(getattr(agent, "inbox_skill", None), "reply_provider", None)
    policy = getattr(provider, "product_lookup", None)
    if policy is None:
        return False, "this runtime has no product lookup policy"
    policy.enabled = bool(value)
    return True, None


# config key -> setter over the live object
HOT_APPLIABLE = {
    "products.lookup_enabled": _product_lookup_setter,
    "products.shipping": _shipping_setter,
    "products.default_product_id": _default_product_setter,
    "messaging.scenario_examples": _scenario_setter,
    "messaging.reply_style": _reply_style_setter,
    "messaging.reply_language": _reply_language_setter,
    "messaging.follow_up.rules": _followup_setter,
    "messaging.auto_check.reply_quiet_period_seconds":
        _inbox_setter("reply_quiet_period_seconds"),
    "messaging.auto_check.reply_wait_timeout_seconds":
        _inbox_setter("reply_wait_timeout_seconds"),
    "messaging.auto_check.reply_wait_poll_seconds":
        _inbox_setter("reply_wait_poll_seconds", minimum=0.5),
    "messaging.auto_check.max_conversations_per_scan":
        _inbox_int_setter("max_conversations_per_scan"),
}

# Known keys that exist but cannot take effect without a restart, with the
# reason, so the dashboard can explain itself instead of just refusing.
RESTART_REQUIRED = {
    "messaging.auto_check.interval_seconds":
        "the watcher's sleep interval is captured when the task starts",
    "browser.cdp_url": "the browser connection is established at boot",
    "storage.database_url": "the database is opened at boot",
}


def apply_runtime_settings(agent, config, values: dict[str, Any]) -> dict[str, Any]:
    """Apply what can be applied now; persist all of it for the next boot."""
    applied, deferred, rejected = {}, {}, {}

    for key, value in values.items():
        setter = HOT_APPLIABLE.get(key)
        if setter is not None:
            ok, error = setter(agent, value)
            if ok:
                applied[key] = value
            else:
                rejected[key] = error
            continue
        if key in RESTART_REQUIRED:
            deferred[key] = RESTART_REQUIRED[key]
            continue
        rejected[key] = "unknown or not settable at runtime"

    # Persist everything the caller was allowed to set, so a restart keeps it.
    persisted = []
    for key in list(applied) + list(deferred):
        try:
            config.set(key, values[key])
            persisted.append(key)
        except Exception as exc:
            logger.warning("[control] setting %s not set", key, exc_info=True)
            rejected[key] = f"could not persist: {type(exc).__name__}"
    if persisted:
        try:
            config.save()
        except Exception as exc:
            logger.warning(
                "[control] settings not persisted error_type=%s", type(exc).__name__,
                exc_info=True,
            )
            for key in persisted:
                rejected.setdefault(key, "could not write the config file")

    if applied:
        logger.info("[control] settings applied: %s", ", ".join(sorted(applied)))
    return {
        "applied": applied,
        "restart_required": deferred,
        "rejected": rejected,
    }

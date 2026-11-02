"""Operator-authored automatic follow-up rules for silent conversations.

Each rule says: once nobody has heard from the customer for at least
`delay_minutes` after the shop's last message in an open conversation, send
`message` - fixed, pre-approved wording, sent exactly as written by
`messaging.followup_scheduler.FollowUpScheduler`, never shown to an LLM.
Distinct from `ScenarioPolicy`, which only shapes what a model writes.

One instance is shared with `FollowUpScheduler` (see main.py), so a dashboard
edit through `control/settings.py` changes what the very next scan enqueues.
"""

import uuid

MAX_RULES = 20
MAX_MESSAGE_CHARACTERS = 1000
MIN_DELAY_MINUTES = 1
MAX_DELAY_MINUTES = 60 * 24 * 14  # 14 days


class FollowUpPolicy:
    def __init__(self, rules=None):
        self.rules = self._normalize(rules)

    @staticmethod
    def _normalize(rules) -> list[dict]:
        if not rules:
            return []
        cleaned = []
        for item in rules:
            if not isinstance(item, dict):
                continue
            message = str(item.get("message") or "").strip()[:MAX_MESSAGE_CHARACTERS]
            if not message:
                continue
            try:
                delay_minutes = int(item.get("delay_minutes"))  # type: ignore[arg-type]  # None -> TypeError, caught below
            except (TypeError, ValueError):
                continue
            delay_minutes = max(MIN_DELAY_MINUTES, min(MAX_DELAY_MINUTES, delay_minutes))
            # Kept stable across edits so `FollowUpScheduler`'s per-rule
            # dedup key does not change (and re-fire) just because the
            # operator edited an unrelated rule elsewhere in the list.
            rule_id = str(item.get("id") or "").strip() or uuid.uuid4().hex[:12]
            cleaned.append({
                "id": rule_id,
                "delay_minutes": delay_minutes,
                "message": message,
                "enabled": bool(item.get("enabled", True)),
            })
            if len(cleaned) >= MAX_RULES:
                break
        return cleaned

    def set(self, rules) -> None:
        self.rules = self._normalize(rules)

    def enabled_rules(self) -> list[dict]:
        return [rule for rule in self.rules if rule["enabled"]]

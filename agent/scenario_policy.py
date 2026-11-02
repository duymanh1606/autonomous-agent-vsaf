"""Operator-authored Khách/Agent example exchanges.

Few-shot examples the shop's own staff write and revise from the dashboard -
"when a customer says X, answer like Y" - distinct from `products.lookup_enabled`
and the fact rules in `ReplyGenerator._rules`, which stay in force regardless:
these examples guide tone and approach only and are never treated as a source
of price, stock, order, refund or payment facts.

One instance is shared by every `ReplyGenerator` (weak and strong LLM), the
same pattern `ProductLookupPolicy` already uses, so an edit through
`control/settings.py` takes effect for both prompts at once, with no restart.
"""

MAX_EXAMPLES = 20
MAX_FIELD_CHARACTERS = 500


class ScenarioPolicy:
    def __init__(self, examples=None):
        self.examples = self._normalize(examples)

    @staticmethod
    def _normalize(examples) -> list[dict]:
        if not examples:
            return []
        cleaned = []
        for item in examples:
            if not isinstance(item, dict):
                continue
            customer = str(item.get("customer") or "").strip()[:MAX_FIELD_CHARACTERS]
            agent = str(item.get("agent") or "").strip()[:MAX_FIELD_CHARACTERS]
            # An example missing either side teaches nothing - drop it rather
            # than inject a one-sided fragment into every reply prompt.
            if not customer or not agent:
                continue
            cleaned.append({"customer": customer, "agent": agent})
            if len(cleaned) >= MAX_EXAMPLES:
                break
        return cleaned

    def set(self, examples) -> None:
        self.examples = self._normalize(examples)

    def render(self) -> str:
        """The few-shot block to inject into a reply prompt, or "" with none set."""
        if not self.examples:
            return ""
        lines = [
            "Shop-provided example customer/agent exchanges - follow this tone and "
            "approach, adapting rather than copying verbatim when the real "
            "conversation differs, and never use them to state a price, stock, "
            "order, refund or payment fact that is not already verified:"
        ]
        for index, example in enumerate(self.examples, start=1):
            lines.append(f"Example {index} customer: {example['customer']}")
            lines.append(f"Example {index} agent: {example['agent']}")
        return "\n".join(lines) + "\n"

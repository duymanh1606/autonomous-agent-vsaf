"""Shop-wide delivery facts: shipping fee, free-shipping threshold, delivery
time, cash on delivery.

They are not a property of one product, and a customer asks about them on
their own ("ship bao nhiêu?") - a question no product lookup is run for. So
they ride along with every reply as verified data (`PipelineReplyProvider`
adds them to the tool result the generator and the validator read): the model
may state them, and `ReplyValidator` accepts the shipping fee and totals
built from it, exactly like a catalogue price.

Configured per workspace as `products.shipping` (Dashboard > Kịch bản >
Giao hàng); unset means no delivery facts, and a reply that states one is
refused like any other unverified amount.
"""

from typing import Any

MAX_TEXT = 120


def normalize_shipping(value) -> dict[str, Any]:
    """The facts as the model and the validator see them; {} when unset.

    Invalid entries are dropped rather than guessed: a fee that is not a
    non-negative number is no fee at all, so a reply quoting one is refused.
    """
    if not isinstance(value, dict):
        return {}
    facts: dict[str, Any] = {}
    fee = value.get("fee")
    if isinstance(fee, (int, float)) and not isinstance(fee, bool) and fee >= 0:
        facts["shipping_fee"] = int(fee) if float(fee).is_integer() else fee
    free_from = value.get("free_from_quantity")
    if isinstance(free_from, int) and not isinstance(free_from, bool) and free_from >= 1:
        facts["free_shipping_from_quantity"] = free_from
    for source, target in (("delivery_days", "delivery_days"), ("area", "delivery_area")):
        text = " ".join(str(value.get(source) or "").split())[:MAX_TEXT]
        if text:
            facts[target] = text
    if isinstance(value.get("cash_on_delivery"), bool):
        facts["cash_on_delivery"] = value["cash_on_delivery"]
    return facts


MAX_AMOUNT = 1_000_000_000
MAX_QUANTITY = 1000


def clean_shipping_setting(value) -> dict[str, Any] | None:
    """What an operator submitted, as stored in `products.shipping`.

    Raises ValueError with a message the dashboard can show. Blank fields are
    left out; an all-blank form clears the setting (None).
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("thông tin giao hàng không hợp lệ")
    cleaned: dict[str, Any] = {}
    fee = value.get("fee")
    if fee not in (None, ""):
        if isinstance(fee, bool) or not isinstance(fee, (int, float)) or not 0 <= fee <= MAX_AMOUNT:
            raise ValueError("phí ship phải là một số không âm")
        cleaned["fee"] = int(fee) if float(fee).is_integer() else fee
    free_from = value.get("free_from_quantity")
    if free_from not in (None, ""):
        if isinstance(free_from, bool) or not isinstance(free_from, int) or not 1 <= free_from <= MAX_QUANTITY:
            raise ValueError("số lượng miễn ship phải là số nguyên từ 1")
        cleaned["free_from_quantity"] = free_from
    for key in ("delivery_days", "area"):
        text = " ".join(str(value.get(key) or "").split())
        if len(text) > MAX_TEXT:
            raise ValueError(f"mỗi ô chữ tối đa {MAX_TEXT} ký tự")
        if text:
            cleaned[key] = text
    if isinstance(value.get("cash_on_delivery"), bool):
        cleaned["cash_on_delivery"] = value["cash_on_delivery"]
    return cleaned or None


class ShopPolicy:
    """Shared between the reply provider and control/settings.py, so a change
    from the dashboard reaches the very next reply."""

    def __init__(self, shipping=None):
        self.set_shipping(shipping)

    def set_shipping(self, value) -> None:
        self.shipping = normalize_shipping(value)

    def facts(self) -> dict[str, Any]:
        return dict(self.shipping)

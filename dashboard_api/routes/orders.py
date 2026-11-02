"""Order routes: an operator-kept order book per workspace.

Split out of dashboard_api/main.py, which had grown to hold every route the
dashboard serves. Access control is unchanged: the auth middleware covers
these paths like any other `/api/` path, and every route still starts with
`_account_or_404`, the same per-account check as the rest of the API.
"""

import math

from fastapi import APIRouter, BackgroundTasks, Body, HTTPException

from integrations.sheet_export import export_order


router = APIRouter()

# What an order must say before it can be saved: who, what, how much, where to.
# Without them the order cannot be packed or shipped, and the order book the
# operator keeps for the shop is only as useful as its least complete row.
# The note stays optional. Checked on create and on full edits; a status-only
# change (set_order_status) is not an edit of these fields and is not held up
# by an older order saved before this rule existed.
REQUIRED_TEXT_FIELDS = {
    "customer_name": "tên khách hàng",
    "items": "sản phẩm / số lượng",
    "shipping_address": "địa chỉ giao hàng",
}


MAX_LINE_ITEMS = 50

# Stock. An order holds its products out of stock from the moment it is
# placed: while it is pending, being packed, shipped or done. Cancelled and
# returned orders give them back. Deleting an order gives them back only if
# the goods cannot have left yet (pending, packing) - deleting the record of
# a shipped or delivered order does not put anything back on the shelf.
# Orders logged before orders touched stock (stock_deducted false) never
# move it either way.
HOLDS_STOCK = frozenset({"pending", "packing", "shipping", "done"})
RESTOCK_ON_DELETE = frozenset({"pending", "packing"})


def _units(lines) -> dict[str, int]:
    units: dict[str, int] = {}
    for line in lines or ():
        if isinstance(line, dict) and line.get("product_id"):
            product_id = str(line["product_id"])
            units[product_id] = units.get(product_id, 0) + int(line.get("quantity") or 0)
    return units


def _held(order: dict) -> dict[str, int]:
    """What this stored order currently holds out of stock."""
    if not order.get("stock_deducted") or order.get("status") not in HOLDS_STOCK:
        return {}
    return _units(order.get("line_items"))


def _adjust_stock(account, changes: dict[str, int]) -> dict[str, int]:
    """Apply {product_id: +returned / -taken}; a shortfall is the caller's 400."""
    changes = {product_id: delta for product_id, delta in changes.items() if delta}
    if not changes:
        return {}
    try:
        _main()._products(account).adjust_stock(changes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return changes


def _undo_stock(account, applied: dict[str, int]) -> None:
    """Put back what _adjust_stock did, when the order write after it failed."""
    if applied:
        _main()._products(account).adjust_stock({k: -v for k, v in applied.items()})


def _line_items(account, fields: dict, stored: list | None = None, held=None) -> dict:
    """Resolve the picked products into the order's line items and `items`.

    The page sends only `{product_id, quantity}` per line. Names and prices
    come from the shop's own catalogue, here, so the order records what the
    shop actually sells at the price it charged - never whatever a request
    claims. A line already on the order (`stored`) keeps the name and price
    it was taken at, so editing an order months later neither reprices it
    nor fails because the product has since been renamed or deleted.

    No `line_items` (or an empty list) leaves `items` as sent: an order
    logged before the picker existed is still editable as the text it has.
    """
    raw = fields.get("line_items")
    if not raw:
        return {**fields, "line_items": []}
    if not isinstance(raw, list) or len(raw) > MAX_LINE_ITEMS:
        raise HTTPException(status_code=400, detail="Danh sách sản phẩm không hợp lệ")
    previous = {
        str(line.get("product_id")): line for line in (stored or [])
        if isinstance(line, dict)
    }
    catalogue = _main()._products(account)
    merged: dict[str, dict] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise HTTPException(status_code=400, detail="Danh sách sản phẩm không hợp lệ")
        product_id = str(entry.get("product_id") or "").strip()
        quantity = entry.get("quantity")
        # bool is an int subclass; True would otherwise pass as 1.
        if (
            isinstance(quantity, bool) or not isinstance(quantity, (int, float))
            or not math.isfinite(quantity) or quantity != int(quantity) or quantity <= 0
        ):
            raise HTTPException(status_code=400, detail="Số lượng phải là số nguyên lớn hơn 0")
        if product_id in merged:
            merged[product_id]["quantity"] += int(quantity)
            continue
        if product_id in previous:
            name = previous[product_id].get("name")
            unit_price = previous[product_id].get("unit_price")
        else:
            product = catalogue.get_by_id(product_id) if product_id else None
            if product is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Không có sản phẩm {product_id or '(trống)'} trong danh mục của shop",
                )
            name, unit_price = product.get("name") or product_id, product.get("price")
        merged[product_id] = {
            "product_id": product_id, "name": name,
            "quantity": int(quantity), "unit_price": unit_price,
        }
    lines = list(merged.values())
    _check_stock(catalogue, lines, previous, held or {})
    return {
        **fields,
        "line_items": lines,
        "items": ", ".join(f"{line['quantity']}x {line['name']}" for line in lines),
    }


def _check_stock(catalogue, lines: list[dict], previous: dict, held: dict) -> None:
    """Refuse a quantity the shop does not have - with a message naming every
    short line. `adjust_stock` makes the binding check under its lock; this
    one is for the message.

    Checked against the product's current stock (a combo's is derived from
    its parts) plus what this order already holds (`held`), since an edit
    only takes the difference. A line already on the order is only checked
    when its quantity goes up. A line whose product has since left the
    catalogue has no stock to compare against and is let through.
    """
    short = []
    for line in lines:
        before = int((previous.get(line["product_id"]) or {}).get("quantity") or 0)
        if line["quantity"] <= before:
            continue
        product = catalogue.get_by_id(line["product_id"])
        if product is None:
            continue
        stock = product.get("stock")
        available = (0 if stock is None else int(stock)) + held.get(line["product_id"], 0)
        if line["quantity"] > available:
            short.append(f"{line['name']} (còn {available}, đặt {line['quantity']})")
    if short:
        raise HTTPException(
            status_code=400, detail="Số lượng vượt tồn kho: " + "; ".join(short),
        )


def _validate_order(fields: dict) -> None:
    missing = [
        label for key, label in REQUIRED_TEXT_FIELDS.items()
        if not str(fields.get(key) or "").strip()
    ]
    amount = fields.get("total_amount")
    if amount is None or amount == "":
        missing.insert(1, "tổng tiền")
    if missing:
        raise HTTPException(
            status_code=400, detail="Thiếu thông tin bắt buộc: " + ", ".join(missing),
        )
    # bool is an int subclass; True would otherwise pass as 1.
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount):
        raise HTTPException(status_code=400, detail="Tổng tiền phải là một số")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Tổng tiền phải lớn hơn 0")


def _main():
    # Late, on purpose. main.py owns the state these routes share with the
    # rest of the dashboard (the account registry and access check, the
    # stores, the search cache) and includes this router while it is still
    # being imported. Looking those up at call time also means a test that
    # patches `dashboard_api.main.<name>` patches what these routes use.
    from dashboard_api import main

    return main


@router.get("/api/accounts/{account_id}/orders")
def read_orders(account_id: str):
    _main()._account_or_404(account_id)
    return _main().orders_store.list(account_id)


@router.get("/api/accounts/{account_id}/orders/{order_id}")
def read_order(account_id: str, order_id: int):
    _main()._account_or_404(account_id)
    order = _main().orders_store.get(account_id, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="unknown order")
    return order


def _export_order(account, order: dict, background_tasks: BackgroundTasks) -> None:
    """Enrich `order` with the customer's TikTok identity (customer_profile,
    looked up by the order's conversation_id) before handing it to the sheet
    export - orders themselves don't store that identity, only a name typed
    by the operator and an optional conversation_id link."""
    identity = _main()._reader(account).customer_identity(order.get("conversation_id"))
    background_tasks.add_task(
        export_order, {**order, "id_tiktok": identity.get("username")}, shop=account.label,
    )


@router.post("/api/accounts/{account_id}/orders")
def create_order(account_id: str, fields: dict = Body(...), background_tasks: BackgroundTasks = None):
    account = _main()._account_or_404(account_id)
    fields = _line_items(account, fields)
    _validate_order(fields)
    status = fields.get("status") or "pending"
    taken = _adjust_stock(account, {
        product_id: -quantity for product_id, quantity in _units(fields["line_items"]).items()
    } if status in HOLDS_STOCK else {})
    fields = {**fields, "stock_deducted": bool(taken)}
    try:
        order = _main().orders_store.create(account_id, fields)
    except Exception as exc:
        _undo_stock(account, taken)
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise
    _main().clear_search_cache(account_id)
    _export_order(account, order, background_tasks)
    return order


@router.put("/api/accounts/{account_id}/orders/{order_id}")
def update_order(
    account_id: str, order_id: int, fields: dict = Body(...), background_tasks: BackgroundTasks = None,
):
    account = _main()._account_or_404(account_id)
    current = _main().orders_store.get(account_id, order_id)
    if current is None:
        raise HTTPException(status_code=404, detail="unknown order")
    held = _held(current)
    fields = _line_items(account, fields, stored=current.get("line_items"), held=held)
    _validate_order(fields)
    changed = {}
    if current.get("stock_deducted"):
        # Only the difference moves: what it held before, back; what it
        # holds now (nothing, if this edit cancels it), out.
        will_hold = (
            _units(fields["line_items"])
            if (fields.get("status") or "pending") in HOLDS_STOCK else {}
        )
        changed = _adjust_stock(account, {
            product_id: held.get(product_id, 0) - will_hold.get(product_id, 0)
            for product_id in set(held) | set(will_hold)
        })
    try:
        order = _main().orders_store.update(account_id, order_id, fields)
    except Exception as exc:
        _undo_stock(account, changed)
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise
    if order is None:
        _undo_stock(account, changed)
        raise HTTPException(status_code=404, detail="unknown order")
    _main().clear_search_cache(account_id)
    _export_order(account, order, background_tasks)
    return order


@router.post("/api/accounts/{account_id}/orders/{order_id}/status")
def set_order_status(
    account_id: str, order_id: int, fields: dict = Body(...), background_tasks: BackgroundTasks = None,
):
    account = _main()._account_or_404(account_id)
    new_status = fields.get("status")
    if not new_status:
        raise HTTPException(status_code=400, detail="status is required")
    current = _main().orders_store.get(account_id, order_id)
    if current is None:
        raise HTTPException(status_code=404, detail="unknown order")
    # Cancelled or returned: what the order held goes back on the shelf.
    returned = _adjust_stock(account, _held(current) if new_status not in HOLDS_STOCK else {})
    try:
        order = _main().orders_store.set_status(account_id, order_id, new_status)
    except Exception as exc:
        _undo_stock(account, returned)
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise
    if order is None:
        _undo_stock(account, returned)
        raise HTTPException(status_code=404, detail="unknown order")
    _main().clear_search_cache(account_id)
    _export_order(account, order, background_tasks)
    return order


@router.delete("/api/accounts/{account_id}/orders/{order_id}")
def delete_order(account_id: str, order_id: int):
    account = _main()._account_or_404(account_id)
    current = _main().orders_store.get(account_id, order_id)
    if current is None or not _main().orders_store.delete(account_id, order_id):
        raise HTTPException(status_code=404, detail="unknown order")
    if current.get("status") in RESTOCK_ON_DELETE:
        _adjust_stock(account, _held(current))
    _main().clear_search_cache(account_id)
    return {"deleted": True}

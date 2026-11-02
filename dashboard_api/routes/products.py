"""Product catalogue routes: products, combos and their media, per workspace.

Split out of dashboard_api/main.py. Each account has its own catalogue
directory (`data/accounts/<id>/products`) so two accounts can sell different
products under the same dashboard - see `main._products()` and
`AccountRegistry.write_config`'s `products.product_root`. Access control is
unchanged: the auth middleware covers these paths, and every route starts
with `_account_or_404`.
"""

import contextlib
import math

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from storage.product_repository import ITEM_TYPE_COMBO


router = APIRouter()

MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_VIDEO_BYTES = 80 * 1024 * 1024


def _main():
    # Late, on purpose. main.py owns the state these routes share with the
    # rest of the dashboard (the account registry and access check, the
    # stores, the search cache) and includes this router while it is still
    # being imported. Looking those up at call time also means a test that
    # patches `dashboard_api.main.<name>` patches what these routes use.
    from dashboard_api import main

    return main


# Each account has its own catalogue directory (`data/accounts/<id>/products`)
# so two accounts can sell different products under the same dashboard - see
# `_main()._products()` and `AccountRegistry.write_config`'s `products.product_root`.

PRODUCT_FIELDS = (
    "name", "brand", "category", "form", "package", "price", "volume",
    "description", "ingredients", "benefits", "warnings", "keywords", "tags",
    "stock",
    "item_type", "cost_price", "currency", "shelf_life_days", "is_active",
    "low_stock_threshold", "combo_items",
)


def _product_fields(payload: dict) -> dict:
    return {key: payload[key] for key in PRODUCT_FIELDS if key in payload}


# Counts and amounts that have no meaning below zero. A negative stock is not
# "oversold", it is a typo - and the agent reads `stock` to answer "còn hàng
# không?", so saving one is how a customer gets told something false.
NON_NEGATIVE_PRODUCT_FIELDS = {
    "stock": "Tồn kho",
    "low_stock_threshold": "Ngưỡng cảnh báo sắp hết",
    "price": "Đơn giá",
    "cost_price": "Giá vốn",
    "shelf_life_days": "Số ngày sử dụng",
}


def _validate_required(product: dict) -> None:
    """What a product must have before the agent can sell it.

    Checked on the product as it will be saved - for an edit, the stored
    product with the edit merged over it, since save_product merges - so a
    partial update cannot leave it incomplete, and an older product missing
    a field is asked for it the next time it is edited. The agent quotes
    `price` and answers "còn hàng không?" from `stock`; `category` sorts the
    catalogue. A combo's stock is derived from its components, so it is not
    asked for. Description stays optional.
    """
    is_combo = product.get("item_type") == ITEM_TYPE_COMBO
    missing = []
    if not str(product.get("name") or "").strip():
        missing.append("tên sản phẩm")
    if not str(product.get("category") or "").strip():
        missing.append("danh mục")
    price = product.get("price")
    if price is None or price == "":
        missing.append("đơn giá")
    if not is_combo and (product.get("stock") is None or product.get("stock") == ""):
        missing.append("tồn kho")
    if missing:
        raise HTTPException(
            status_code=400, detail="Thiếu thông tin bắt buộc: " + ", ".join(missing),
        )
    # bool is an int subclass; True would otherwise pass as 1.
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price):
        raise HTTPException(status_code=400, detail="Đơn giá phải là một số")
    if price <= 0:
        raise HTTPException(status_code=400, detail="Đơn giá phải lớn hơn 0")


def _validate_non_negative(fields: dict) -> None:
    for key, label in NON_NEGATIVE_PRODUCT_FIELDS.items():
        value = fields.get(key)
        if value is None:
            continue
        # bool is an int subclass; True would otherwise pass as 1.
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise HTTPException(status_code=400, detail=f"{label} phải là một số")
        if value < 0:
            raise HTTPException(status_code=400, detail=f"{label} không được nhỏ hơn 0")


def _validate_combo(repo, product_id: str, fields: dict) -> None:
    """Reject a combo whose parts cannot actually be assembled.

    Availability is derived from the components (see
    `ProductRepository._resolve_combo_stock`), so a broken component list does
    not fail loudly - it silently resolves to "0 sellable", or worse, to a
    combo that quotes a price for something the shop cannot pack. Catching it
    at the edit that introduces it is the only place a person is there to fix
    it.
    """
    if fields.get("item_type") != ITEM_TYPE_COMBO:
        return
    entries = fields.get("combo_items")
    if not isinstance(entries, list) or not entries:
        raise HTTPException(
            status_code=400, detail="combo cần ít nhất một sản phẩm lẻ",
        )
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise HTTPException(status_code=400, detail="thành phần combo không hợp lệ")
        component_id = str(entry.get("product_id") or "").strip()
        try:
            quantity = float(entry.get("quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0
        if not component_id or quantity <= 0:
            raise HTTPException(
                status_code=400,
                detail="mỗi thành phần cần một sản phẩm lẻ và số lượng lớn hơn 0",
            )
        if component_id.casefold() == str(product_id).casefold():
            raise HTTPException(
                status_code=400, detail="combo không thể chứa chính nó",
            )
        if component_id.casefold() in seen:
            raise HTTPException(
                status_code=400,
                detail=f"thành phần bị lặp: {component_id}",
            )
        seen.add(component_id.casefold())
        component = repo.get_by_id(component_id)
        if component is None:
            raise HTTPException(
                status_code=400, detail=f"không có sản phẩm lẻ: {component_id}",
            )
        if component.get("item_type") == ITEM_TYPE_COMBO:
            raise HTTPException(
                status_code=400,
                detail=f"thành phần phải là sản phẩm lẻ, không phải combo: {component_id}",
            )


@router.get("/api/accounts/{account_id}/products")
def read_products(account_id: str):
    account = _main()._account_or_404(account_id)
    repo = _main()._products(account)
    return {
        "products": repo.load_products(),
        "missing_media": repo.missing_media(),
        "invalid_product_directories": repo.invalid_product_directories(),
        **_default_product_view(account, repo),
    }


def _default_product_view(account, repo) -> dict:
    # `default_product_id` is what the shop chose (None = automatic);
    # `effective_default_product_id` is what the agent actually uses for a
    # question naming no product - the same ProductRepository.default_product_id
    # the agent calls, so a chosen product that was since deleted, stopped or
    # turned into a combo shows as not in effect instead of looking set.
    configured = _main().registry.config(account).get("products.default_product_id")
    return {
        "default_product_id": configured,
        "effective_default_product_id": repo.default_product_id(configured),
    }


@router.put("/api/accounts/{account_id}/default-product")
async def write_default_product(account_id: str, product_id: str | None = Body(..., embed=True)):
    """The product a customer question naming none is about.

    null = automatic (the only single item on sale). Stored and pushed like
    the shipping facts: file first, then live to a running agent.
    """
    account = _main()._account_or_404(account_id)
    repo = _main()._products(account)
    wanted = (product_id or "").strip() or None
    if wanted is not None:
        product = repo.get_by_id(wanted)
        if product is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy sản phẩm")
        if product.get("item_type") == ITEM_TYPE_COMBO:
            raise HTTPException(status_code=422, detail="Combo không thể là sản phẩm mặc định")
        if product.get("is_active") is False:
            raise HTTPException(status_code=422, detail="Sản phẩm đang ngừng bán")
    cfg = _main().registry.config(account)
    cfg.set("products.default_product_id", wanted)
    cfg.save()
    with contextlib.suppress(HTTPException):
        await _main()._control(account, "PATCH", "/control/settings",
                               {"values": {"products.default_product_id": wanted}})
    return _default_product_view(account, repo)


@router.get("/api/accounts/{account_id}/products/{product_id}")
def read_product(account_id: str, product_id: str):
    product = _main()._products(_main()._account_or_404(account_id)).get_by_id(product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="unknown product")
    return product


@router.post("/api/accounts/{account_id}/products")
def create_product(
    account_id: str,
    product_id: str = Body(..., embed=True),
    fields: dict = Body(..., embed=True),
):
    repo = _main()._products(_main()._account_or_404(account_id))
    slug = str(product_id or "").strip()
    if not slug or not slug.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(
            status_code=400,
            detail="product_id must be letters/digits/-/_ only",
        )
    if repo.get_by_id(slug) is not None:
        raise HTTPException(status_code=409, detail="product already exists")
    cleaned = _product_fields(fields)
    _validate_non_negative(cleaned)
    _validate_required(cleaned)
    _validate_combo(repo, slug, cleaned)
    try:
        product = repo.save_product(slug, cleaned)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return product


@router.put("/api/accounts/{account_id}/products/{product_id}")
def update_product(account_id: str, product_id: str, fields: dict = Body(...)):
    repo = _main()._products(_main()._account_or_404(account_id))
    existing = repo.get_by_id(product_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="unknown product")
    cleaned = _product_fields(fields)
    _validate_non_negative(cleaned)
    # Switching an item's type after it has been sold rewrites what its stock
    # figure ever meant - a single's number is a count on a shelf, a combo's
    # is derived from other products entirely - so history stops matching the
    # goods. The form locks the control; this is what makes the rule real.
    if (
        "item_type" in cleaned
        and cleaned["item_type"] != existing.get("item_type")
    ):
        raise HTTPException(
            status_code=400,
            detail="không thể đổi loại mặt hàng sau khi đã tạo",
        )
    cleaned.setdefault("item_type", existing.get("item_type"))
    _validate_required({**existing, **cleaned})
    _validate_combo(repo, product_id, cleaned)
    try:
        product = repo.save_product(product_id, cleaned)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return product


@router.delete("/api/accounts/{account_id}/products/{product_id}")
def delete_product(account_id: str, product_id: str):
    deleted = _main()._products(_main()._account_or_404(account_id)).delete_product(product_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="unknown product")
    return {"deleted": True}


@router.post("/api/accounts/{account_id}/products/{product_id}/media")
async def upload_product_media(
    account_id: str,
    product_id: str,
    media_type: str = Form(...),
    file: UploadFile = File(...),
):
    if media_type not in {"image", "video"}:
        raise HTTPException(status_code=400, detail="media_type must be image or video")
    repo = _main()._products(_main()._account_or_404(account_id))
    if repo.get_by_id(product_id) is None:
        raise HTTPException(status_code=404, detail="unknown product")

    limit = MAX_IMAGE_BYTES if media_type == "image" else MAX_VIDEO_BYTES
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(status_code=413, detail="file too large")

    try:
        repo.save_media(
            product_id, media_type, file.filename or "upload", content,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return repo.get_by_id(product_id)


@router.get("/api/accounts/{account_id}/products/{product_id}/media/{relative_path:path}")
def read_product_media(account_id: str, product_id: str, relative_path: str):
    repo = _main()._products(_main()._account_or_404(account_id))
    directory = repo.product_directory(product_id)
    if directory is None or not directory.is_dir():
        raise HTTPException(status_code=404, detail="unknown product")
    candidate = (directory / relative_path).resolve()
    try:
        candidate.relative_to(directory.resolve())
    except ValueError:
        raise HTTPException(status_code=404, detail="not found") from None
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(candidate)


@router.delete("/api/accounts/{account_id}/products/{product_id}/media")
def delete_product_media(
    account_id: str,
    product_id: str,
    relative_path: str = Body(..., embed=True),
):
    repo = _main()._products(_main()._account_or_404(account_id))
    removed = repo.delete_media(product_id, relative_path)
    if not removed:
        raise HTTPException(status_code=404, detail="unknown media")
    return repo.get_by_id(product_id)

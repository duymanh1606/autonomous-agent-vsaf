import json
import logging
import os
import shutil
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from load_config import BASE_DIR


logger = logging.getLogger(__name__)


IMAGE_SUFFIXES = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
}

VIDEO_SUFFIXES = {
    ".mp4",
    ".mov",
    ".webm",
    ".m4v",
}


# Product fields used for keyword search.
SEARCHABLE_FIELDS = (
    "name",
    "brand",
    "category",
    "form",
    "package",
    "volume",
    "description",
)

SEARCHABLE_LISTS = (
    "ingredients",
    "benefits",
    "keywords",
    "tags",
)


# A combo is sold as one line but held in stock as its parts. Only a single
# item may be a component: allowing a combo to contain a combo would make
# "how many can we still sell" a graph walk with cycles to defend against,
# for no case the catalogue actually has.
ITEM_TYPE_SINGLE = "single"
ITEM_TYPE_COMBO = "combo"
ITEM_TYPES = (ITEM_TYPE_SINGLE, ITEM_TYPE_COMBO)


def _clean_combo_items(value: Any) -> list[dict[str, Any]]:
    """The `[{product_id, quantity}]` rows of a combo, as far as they parse.

    Rows without a component or without a positive quantity are dropped
    rather than kept as zeroes: a component that contributes nothing would
    otherwise read as "unlimited" when availability is worked out from the
    parts, and quietly promise a combo nobody can actually assemble.
    """
    if not isinstance(value, list):
        return []
    items = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        product_id = str(entry.get("product_id") or "").strip()
        try:
            quantity = float(entry.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
        if not product_id or quantity <= 0:
            continue
        items.append({"product_id": product_id, "quantity": quantity})
    return items


class ProductRepository:
    """Repository for products stored in individual directories, one per
    account (`product_root` is the account's own catalogue directory)."""

    def __init__(
        self,
        product_root: str | Path = "data/products",
    ):
        self.product_root = Path(product_root)

        if not self.product_root.is_absolute():
            self.product_root = BASE_DIR / self.product_root

        # Cache is maintained per product instead of for the entire catalogue.
        self._cache: dict[str, dict[str, Any]] = {}
        self._mtimes: dict[str, float] = {}
        # Held across the read-check-write of adjust_stock, so two orders
        # placed at once cannot both take the last unit.
        self._stock_lock = threading.RLock()


    def _product_directory(
        self,
        product_id: str,
    ) -> Path | None:
        """Return the direct child directory belonging to a safe product id.

        Product ids can originate in model-generated tool arguments. They must
        never be allowed to escape ``product_root`` and read an unrelated
        ``product.json`` elsewhere on disk.
        """

        value = str(product_id or "").strip()

        if (
            not value
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
            or Path(value).name != value
        ):
            return None

        return self.product_root / value

    def _product_file(
        self,
        product_id: str,
    ) -> Path | None:
        """Return product.json path belonging to a product."""

        directory = self._product_directory(product_id)

        return directory / "product.json" if directory is not None else None

    def product_directory(self, product_id: str) -> Path | None:
        """Public accessor for a product's own directory - used by the API
        layer to safely serve/validate an uploaded media file's path."""
        return self._product_directory(product_id)

    def _invalidate(self, product_id: str) -> None:
        key = product_id.casefold()
        self._cache.pop(key, None)
        self._mtimes.pop(key, None)

    def _read_raw(self, product_id: str) -> dict[str, Any]:
        file = self._product_file(product_id)
        if file is None or not file.is_file():
            return {}
        try:
            with file.open("r", encoding="utf-8") as stream:
                raw = json.load(stream)
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write_raw(self, product_id: str, raw: dict[str, Any]) -> None:
        directory = self._product_directory(product_id)
        if directory is None:
            raise ValueError(f"invalid product id: {product_id!r}")
        directory.mkdir(parents=True, exist_ok=True)
        # Written aside and renamed into place: the agent is another process
        # reading these files, and an in-place rewrite shows it a truncated
        # file - which reads as "no such product" - for the moment it takes.
        target = directory / "product.json"
        temporary = directory / f".product.json.{os.getpid()}.{threading.get_ident()}.tmp"
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(raw, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, target)
        self._invalidate(product_id)

    def adjust_stock(self, changes: dict[str, int]) -> dict[str, int]:
        """Apply stock changes, all or none: {product_id: +units or -units}.

        A combo has no stock of its own (it is derived from its parts), so a
        change to a combo becomes a change to each component, times that
        component's quantity per combo. Taking stock is refused - nothing
        written - if any single item would go below zero; returning stock to
        a product that has since been deleted is skipped, there is nowhere to
        put it. Returns the new stock of every single item changed.
        """
        with self._stock_lock:
            units: dict[str, int] = {}
            for product_id, delta in changes.items():
                delta = int(delta)
                if not delta:
                    continue
                product = self._load_normalised(product_id)
                if product is None:
                    if delta < 0:
                        raise ValueError(f"không có sản phẩm {product_id}")
                    continue
                if product.get("item_type") == ITEM_TYPE_COMBO:
                    for component in product.get("combo_items") or []:
                        component_id = str(component.get("product_id") or "")
                        per_combo = int(component.get("quantity") or 0)
                        if component_id and per_combo > 0:
                            units[component_id] = units.get(component_id, 0) + delta * per_combo
                else:
                    units[product_id] = units.get(product_id, 0) + delta

            planned: dict[str, int] = {}
            short: list[str] = []
            for product_id, delta in units.items():
                raw = self._read_raw(product_id)
                if not raw:
                    if delta < 0:
                        raise ValueError(f"không có sản phẩm {product_id}")
                    continue
                current = int(raw.get("stock") or 0)
                if current + delta < 0:
                    short.append(f"{raw.get('name') or product_id} (còn {current}, cần {-delta})")
                planned[product_id] = current + delta
            if short:
                raise ValueError("Số lượng vượt tồn kho: " + "; ".join(short))
            for product_id, stock in planned.items():
                raw = self._read_raw(product_id)
                raw["stock"] = stock
                self._write_raw(product_id, raw)
            return planned

    def save_product(
        self,
        product_id: str,
        fields: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Create or update a product's metadata (never touches its media
        list - that goes through `save_media`/`delete_media` instead, so a
        metadata-only edit never drops previously uploaded files)."""

        directory = self._product_directory(product_id)
        if directory is None:
            raise ValueError(f"invalid product id: {product_id!r}")

        raw = self._read_raw(product_id)
        existing_media = raw.get("media")
        raw.update(fields)
        raw["id"] = product_id
        raw["media"] = existing_media if isinstance(existing_media, dict) else {
            "images": [], "videos": [],
        }
        self._write_raw(product_id, raw)
        return self.get_by_id(product_id)

    def delete_product(self, product_id: str) -> bool:
        directory = self._product_directory(product_id)
        if directory is None or not directory.is_dir():
            return False
        shutil.rmtree(directory)
        self._invalidate(product_id)
        return True

    def save_media(
        self,
        product_id: str,
        media_type: str,
        filename: str,
        content: bytes,
    ) -> str:
        """Write an uploaded file into the product's own directory and
        register it in product.json. Returns the new relative path."""

        directory = self._product_directory(product_id)
        if directory is None:
            raise ValueError(f"invalid product id: {product_id!r}")
        if not directory.is_dir():
            raise ValueError(f"unknown product: {product_id!r}")

        allowed = IMAGE_SUFFIXES if media_type == "image" else VIDEO_SUFFIXES
        subdir = "images" if media_type == "image" else "videos"
        suffix = Path(filename).suffix.lower()
        if media_type not in {"image", "video"} or suffix not in allowed:
            raise ValueError(f"unsupported {media_type} file: {filename!r}")

        target_dir = directory / subdir
        target_dir.mkdir(parents=True, exist_ok=True)
        # A random name sidesteps both collisions and any path-traversal
        # attempt hiding in the uploaded filename.
        safe_name = f"{uuid4().hex[:16]}{suffix}"
        (target_dir / safe_name).write_bytes(content)
        relative_path = f"{subdir}/{safe_name}"

        raw = self._read_raw(product_id)
        media = raw.get("media")
        if not isinstance(media, dict):
            media = {"images": [], "videos": []}
        media.setdefault(subdir, [])
        if not isinstance(media[subdir], list):
            media[subdir] = []
        media[subdir].append(relative_path)
        raw["media"] = media
        self._write_raw(product_id, raw)
        return relative_path

    def delete_media(self, product_id: str, relative_path: str) -> bool:
        """Remove one media entry: the file on disk and its product.json
        reference. `relative_path` is validated to stay inside the
        product's own directory before anything is touched."""

        directory = self._product_directory(product_id)
        if directory is None or not directory.is_dir():
            return False

        candidate = (directory / relative_path).resolve()
        try:
            candidate.relative_to(directory.resolve())
        except ValueError:
            return False

        raw = self._read_raw(product_id)
        media = raw.get("media")
        changed = False
        if isinstance(media, dict):
            for key in ("images", "videos"):
                values = media.get(key)
                if isinstance(values, list) and relative_path in values:
                    values.remove(relative_path)
                    changed = True
        if candidate.is_file():
            candidate.unlink()
            changed = True
        if changed:
            raw["media"] = media if isinstance(media, dict) else {"images": [], "videos": []}
            self._write_raw(product_id, raw)
        return changed

    def load_products(
        self,
    ) -> list[dict[str, Any]]:
        """Load every valid product under product_root."""

        if not self.product_root.is_dir():
            logger.warning(
                "[products] product root does not exist: %s",
                self.product_root,
            )
            return []

        products: list[dict[str, Any]] = []

        for directory in sorted(
            self.product_root.iterdir()
        ):
            if not directory.is_dir():
                continue

            product_file = directory / "product.json"

            if not product_file.is_file():
                continue

            product = self.get_by_id(directory.name)

            if product is not None:
                products.append(product)

        return products

    def get_by_id(
        self,
        product_id: str,
    ) -> dict[str, Any] | None:
        """Load one product by directory/product id.

        A combo's stock is resolved here rather than stored, and deliberately
        outside the cache below: it is a fact about this product's *parts*, so
        selling a component changes it while the combo's own file - and
        therefore its cache entry - never moves.
        """
        return self._resolve_combo_stock(self._load_normalised(product_id))

    def _load_normalised(
        self,
        product_id: str,
    ) -> dict[str, Any] | None:
        """One product exactly as its own `product.json` describes it."""

        wanted = str(product_id or "").strip()

        if not wanted:
            return None

        product_file = self._product_file(wanted)

        if product_file is None or not product_file.is_file():
            return None

        try:
            mtime = product_file.stat().st_mtime
        except OSError:
            return None

        cache_key = wanted.casefold()

        if (
            cache_key in self._cache
            and self._mtimes.get(cache_key) == mtime
        ):
            return self._cache[cache_key]

        try:
            with product_file.open(
                "r",
                encoding="utf-8",
            ) as file:
                raw = json.load(file)

        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "[products] unable to read product=%s error=%s",
                wanted,
                type(exc).__name__,
            )
            return None

        if not isinstance(raw, dict):
            logger.warning(
                "[products] product.json is not an object: %s",
                product_file,
            )
            return None

        product = self._normalise(
            raw,
            product_directory=product_file.parent,
        )

        json_id = str(product.get("id") or "").strip()

        if not json_id:
            logger.warning(
                "[products] missing product id in %s",
                product_file,
            )
            return None

        if json_id.casefold() != wanted.casefold():
            logger.warning(
                "[products] directory id mismatch: "
                "directory=%s json_id=%s",
                wanted,
                json_id,
            )

        self._cache[cache_key] = product
        self._mtimes[cache_key] = mtime

        return product

    def _resolve_combo_stock(
        self,
        product: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """A combo's `stock` as the number of combos still assemblable.

        Every consumer already reads `stock` to answer "còn hàng?" - the
        inventory tool, the dashboard, the reply pipeline - so a combo
        reporting its sellable count through that same key keeps all of them
        correct without knowing combos exist.

        Components are read with `_load_normalised`, not `get_by_id`: a
        component is a single item by construction, and going through the
        plain loader makes that structural rather than something a malformed
        catalogue could turn into unbounded recursion.
        """
        if product is None or product.get("item_type") != ITEM_TYPE_COMBO:
            return product

        # A copy: `product` is the cached object, and the resolved count
        # belongs to this read only.
        resolved = dict(product)
        components = product.get("combo_items") or []
        if not components:
            # Nothing declared to build it from - not "unlimited".
            resolved["stock"] = 0
            return resolved

        sellable = []
        for entry in components:
            component = self._load_normalised(entry["product_id"])
            if component is None:
                # A component that no longer exists cannot be assembled, and
                # claiming otherwise would sell a combo that cannot be packed.
                sellable.append(0)
                continue
            try:
                available = float(component.get("stock") or 0)
            except (TypeError, ValueError):
                available = 0.0
            sellable.append(int(max(0.0, available) // entry["quantity"]))

        resolved["stock"] = min(sellable)
        return resolved


    def _normalise(
        self,
        product: dict[str, Any],
        product_directory: Path,
    ) -> dict[str, Any]:
        """Normalise product fields and resolve media paths."""

        item = dict(product)

        item.setdefault("id", "")
        item.setdefault("name", "")
        item.setdefault("brand", "")
        item.setdefault("category", "")
        item.setdefault("form", "")
        item.setdefault("package", "")
        item.setdefault("price", None)
        item.setdefault("volume", "")
        item.setdefault("description", "")
        item.setdefault("ingredients", [])
        item.setdefault("benefits", [])
        item.setdefault("warnings", [])
        item.setdefault("keywords", [])
        item.setdefault("tags", [])
        item.setdefault("stock", 0)

        # Catalogue entries that predate combos carry none of these keys, so
        # every default here is also the "behaves exactly as before" value:
        # a plain single item, sellable, with no cost/expiry/threshold set.
        item["item_type"] = (
            str(item.get("item_type") or "").strip().casefold()
            if str(item.get("item_type") or "").strip().casefold() in ITEM_TYPES
            else ITEM_TYPE_SINGLE
        )
        item.setdefault("cost_price", None)
        item.setdefault("currency", "")
        item.setdefault("shelf_life_days", None)
        item.setdefault("low_stock_threshold", None)
        # Only an explicit False switches a product off, so a catalogue
        # written before this field existed stays on sale.
        item["is_active"] = item.get("is_active", True) is not False
        item["combo_items"] = _clean_combo_items(item.get("combo_items"))

        raw_media = item.get("media")

        if not isinstance(raw_media, dict):
            raw_media = {}

        raw_images = raw_media.get("images") or []
        raw_videos = raw_media.get("videos") or []

        if not isinstance(raw_images, list):
            raw_images = []

        if not isinstance(raw_videos, list):
            raw_videos = []

        images = []

        for value in raw_images:
            media = self._resolve_media(
                product_directory=product_directory,
                media_value=value,
                media_type="image",
            )

            if media is not None:
                images.append(media)

        videos = []

        for value in raw_videos:
            media = self._resolve_media(
                product_directory=product_directory,
                media_value=value,
                media_type="video",
            )

            if media is not None:
                videos.append(media)

        item["media"] = {
            "images": images,
            "videos": videos,
        }

        # Internal information useful for repository operations.
        item["_directory"] = str(
            product_directory.resolve()
        )

        return item


    def _resolve_media(
        self,
        product_directory: Path,
        media_value: Any,
        media_type: str,
    ) -> dict[str, Any] | None:
        """Resolve a media entry from product.json to a local file."""

        if not isinstance(media_value, str):
            return None

        value = media_value.strip()

        if not value:
            return None

        path = Path(value)

        if not path.is_absolute():
            path = product_directory / path

        try:
            resolved = path.resolve()
        except OSError:
            resolved = path

        exists = resolved.is_file()

        suffix = resolved.suffix.lower()

        if media_type == "image":
            valid_type = suffix in IMAGE_SUFFIXES

        elif media_type == "video":
            valid_type = suffix in VIDEO_SUFFIXES

        else:
            valid_type = False

        if not valid_type:
            logger.warning(
                "[products] unsupported %s media file: %s",
                media_type,
                resolved,
            )

        size = 0

        if exists:
            try:
                size = resolved.stat().st_size
            except OSError:
                size = 0

        return {
            "type": media_type,
            "name": resolved.name,
            "relative_path": value,
            "path": str(resolved),
            "exists": exists,
            "valid_type": valid_type,
            "size": size,
            "source": "disk",
        }

    def get_images(
        self,
        product_id: str,
        only_existing: bool = True,
    ) -> list[dict[str, Any]]:
        """Return images belonging to a product."""

        product = self.get_by_id(product_id)

        if product is None:
            return []

        images = product.get(
            "media",
            {},
        ).get(
            "images",
            [],
        )

        if not only_existing:
            return images

        return [
            image
            for image in images
            if image.get("exists")
            and image.get("valid_type")
        ]

    def get_videos(
        self,
        product_id: str,
        only_existing: bool = True,
    ) -> list[dict[str, Any]]:
        """Return videos belonging to a product."""

        product = self.get_by_id(product_id)

        if product is None:
            return []

        videos = product.get(
            "media",
            {},
        ).get(
            "videos",
            [],
        )

        if not only_existing:
            return videos

        return [
            video
            for video in videos
            if video.get("exists")
            and video.get("valid_type")
        ]

    def get_primary_image(
        self,
        product_id: str,
    ) -> str | None:
        """Return the first usable image path."""

        images = self.get_images(product_id)

        if not images:
            return None

        return str(images[0]["path"])

    def get_primary_video(
        self,
        product_id: str,
    ) -> str | None:
        """Return the first usable video path."""

        videos = self.get_videos(product_id)

        if not videos:
            return None

        return str(videos[0]["path"])



    def search(
        self,
        query: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Search products using simple keyword scoring."""

        query = str(query or "").strip().casefold()
        products = self._active_products()
        if not query:
            return products[:limit]
        scored = self._scored(query, products)
        return [product for _, product in scored[:limit]]

    # Below this share of the query's words matched, a hit is a coincidence
    # of one common word ("sữa", "nước"), not the product the customer named.
    RESOLVE_MIN_COVERAGE = 0.6

    def resolve(self, query: str) -> dict[str, Any] | None:
        """The one product a customer's words unambiguously name, or None.

        `search` ranks anything sharing a single word, which is right for
        browsing and wrong for the reply pipeline: "sữa chua 100g" against a
        catalogue of Milo, Ông Thọ and sữa tắm returned Milo, and the agent
        sent a Milo photo for a yogurt request. So a match here needs most of
        the customer's words to hit, and no other product tied for first -
        a tie ("sữa" alone) is ambiguity, and ambiguity is a handoff, not a
        guess.
        """
        query = str(query or "").strip().casefold()
        if not query:
            return None
        words = [word for word in query.split() if word]
        scored = self._scored(query, self._active_products())
        if not scored:
            return None
        best_score, best = scored[0]
        if len(scored) > 1 and scored[1][0] == best_score:
            return None
        # An exact id/name match carries the +10 boost and needs no coverage.
        if best_score >= 10:
            return best
        if best_score / max(1, len(words)) < self.RESOLVE_MIN_COVERAGE:
            return None
        return best

    def default_product_id(self, configured: str | None = None) -> str | None:
        """The product a question that names none is about, or None.

        Most shops sell one product, so "how much is it?" can only mean that
        one. In order: the product the shop set as its default
        (`products.default_product_id`), if it is still a single item on
        sale; else the only single item on sale. A combo is never the
        default - it is listed with its parts' `offers` instead. None when
        neither applies: the agent then asks which product the customer means.
        """
        wanted = str(configured or "").strip()
        if wanted:
            product = self.get_by_id(wanted)
            if (
                product is not None and product.get("is_active", True)
                and product.get("item_type", ITEM_TYPE_SINGLE) != ITEM_TYPE_COMBO
            ):
                return str(product.get("id") or wanted)
        singles = [
            product for product in self._active_products()
            if product.get("item_type", ITEM_TYPE_SINGLE) != ITEM_TYPE_COMBO
        ]
        return str(singles[0].get("id")) if len(singles) == 1 else None

    def _active_products(self) -> list[dict[str, Any]]:
        # "Đang sử dụng" unchecked means the shop has stopped selling this.
        # Search is what the reply pipeline offers customers from, so a
        # withdrawn product has to disappear here or the agent keeps quoting
        # and recommending it. It stays readable by id (`get_by_id`) so past
        # orders and open conversations about it still resolve.
        return [
            product for product in self.load_products()
            if product.get("is_active", True)
        ]

    def _scored(
        self, query: str, products: list[dict[str, Any]],
    ) -> list[tuple[int, dict[str, Any]]]:
        words = [
            word
            for word in query.split()
            if word
        ]

        scored: list[
            tuple[int, dict[str, Any]]
        ] = []

        for product in products:

            searchable_values: list[str] = []

            for field in SEARCHABLE_FIELDS:
                value = product.get(field)

                if value is not None:
                    searchable_values.append(
                        str(value)
                    )

            for field in SEARCHABLE_LISTS:
                values = product.get(field) or []

                if isinstance(values, list):
                    searchable_values.extend(
                        str(value)
                        for value in values
                    )

            haystack = " ".join(
                searchable_values
            ).casefold()

            score = sum(
                1
                for word in words
                if word in haystack
            )

            # Stronger boost for an exact name match.
            name = str(
                product.get("name", "")
            ).casefold()

            product_id = str(
                product.get("id", "")
            ).casefold()

            if query == name:
                score += 10

            if query == product_id:
                score += 10

            if score > 0:
                scored.append(
                    (score, product)
                )

        scored.sort(
            key=lambda item: item[0],
            reverse=True,
        )

        return scored


    def missing_media(
        self,
    ) -> list[dict[str, Any]]:
        """Return media referenced by product.json but missing on disk."""

        missing: list[dict[str, Any]] = []

        for product in self.load_products():

            product_id = str(
                product.get("id", "")
            )

            media = product.get(
                "media",
                {},
            )

            for media_type in (
                "images",
                "videos",
            ):
                for item in media.get(
                    media_type,
                    [],
                ):
                    if item.get("exists"):
                        continue

                    missing.append({
                        "product_id": product_id,
                        "type": media_type,
                        "path": item.get("path"),
                    })

        return missing

    def invalid_product_directories(
        self,
    ) -> list[dict[str, Any]]:
        """Return product folders that do not contain product.json."""

        invalid: list[dict[str, Any]] = []

        if not self.product_root.is_dir():
            return invalid

        for directory in sorted(
            self.product_root.iterdir()
        ):
            if not directory.is_dir():
                continue

            product_file = (
                directory
                / "product.json"
            )

            if product_file.is_file():
                continue

            invalid.append({
                "directory": directory.name,
                "reason": "missing_product_json",
            })

        return invalid


    async def get_product(
        self,
        product_id: str,
    ) -> dict[str, Any]:
        """Return structured product data for the agent/tool layer.

        An empty dictionary is returned when the product cannot be found.
        This allows ReplyValidator or another policy layer to reject replies
        that would otherwise invent product information.
        """

        product = self.get_by_id(product_id)

        if product is None:
            product = self.resolve(product_id)

        if product is None:
            return {}

        return {
            "id": product.get("id"),
            "name": product.get("name"),
            "brand": product.get("brand"),
            "category": product.get("category"),
            "form": product.get("form"),
            "package": product.get("package"),
            "volume": product.get("volume"),
            "description": product.get(
                "description"
            ),
            "ingredients": product.get(
                "ingredients",
                [],
            ),
            "benefits": product.get(
                "benefits",
                [],
            ),
            "warnings": product.get(
                "warnings",
                [],
            ),
            "price": product.get("price"),
            "stock": product.get("stock"),
            "item_type": product.get("item_type", ITEM_TYPE_SINGLE),
            # The combos this item is sold in, so "show the combo offers
            # first" has verified prices to show.
            "offers": self._offers_for(product),
            "media": product.get(
                "media",
                {
                    "images": [],
                    "videos": [],
                },
            ),
        }

    def _offers_for(self, product: dict[str, Any]) -> list[dict[str, Any]]:
        """Active combos that contain this single item, cheapest first.

        Only what a customer may be told: name, price and contents - never
        cost price or stock of the combo's other parts.
        """
        if product.get("item_type") == ITEM_TYPE_COMBO:
            return []
        product_id = str(product.get("id") or "")
        offers = []
        for combo in self._active_products():
            if combo.get("item_type") != ITEM_TYPE_COMBO:
                continue
            items = combo.get("combo_items") or []
            if not any(item["product_id"] == product_id for item in items):
                continue
            offers.append({
                "id": combo.get("id"),
                "name": combo.get("name"),
                "price": combo.get("price"),
                "description": combo.get("description"),
                "items": [
                    {"product_id": item["product_id"], "quantity": item["quantity"]}
                    for item in items
                ],
            })
        return sorted(offers, key=lambda offer: (offer["price"] is None, offer["price"] or 0))

    async def check_inventory(
        self,
        product_id: str,
        size: str | None = None,
    ) -> dict[str, Any]:
        """Return verified availability for a product or one size variant.

        The current catalogue stores total stock. A size-specific question is
        answered only when the product explicitly provides a ``variants`` list;
        returning an empty result otherwise makes the reply pipeline hand the
        question to a person instead of inventing size availability.
        """

        product = self.get_by_id(product_id)

        if product is None:
            product = self.resolve(product_id)

        if product is None:
            return {}

        requested_size = str(size or "").strip()
        if requested_size:
            variants = product.get("variants") or []
            if not isinstance(variants, list):
                return {}
            wanted = requested_size.casefold()
            variant = next((
                item for item in variants
                if isinstance(item, dict)
                and str(item.get("size") or "").strip().casefold() == wanted
            ), None)
            if variant is None:
                return {}
            stock = max(0, int(variant.get("stock") or 0))
            return {
                "product_id": product.get("id"),
                "name": product.get("name"),
                "size": variant.get("size"),
                "stock": stock,
                "available": stock > 0,
            }

        stock = max(0, int(product.get("stock") or 0))
        return {
            "product_id": product.get("id"),
            "name": product.get("name"),
            "stock": stock,
            "available": stock > 0,
        }

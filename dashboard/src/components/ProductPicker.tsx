import { useId, useMemo, useState } from "react";
import type { Product } from "../lib/api";
import { formatMoney } from "../lib/format";
import { searchProducts } from "../lib/productSearch";

type Props = {
  products: Product[];
  loading?: boolean;
  onPick: (product: Product) => void;
  /** id of the element that labels this field. */
  labelledBy: string;
  invalid?: boolean;
};

/** A product that cannot be ordered at all right now: none left. */
export function isSoldOut(product: Product): boolean {
  return product.stock != null && product.stock <= 0;
}

/**
 * Search the shop's catalogue and pick a product - a combobox: type to
 * filter (accents optional), arrows to move, Enter to pick, Escape to close.
 * Picking closes the list and clears the box; typing or the down arrow
 * opens it again for the next product. A sold-out product is listed (so the
 * operator sees why it is missing) but cannot be picked.
 */
export function ProductPicker({ products, loading, onPick, labelledBy, invalid }: Props) {
  const listId = useId();
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const matches = useMemo(() => searchProducts(products, query), [products, query]);

  const pick = (product: Product | undefined) => {
    if (!product || isSoldOut(product)) return;
    onPick(product);
    setQuery("");
    setActive(0);
    setOpen(false);
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setOpen(true);
      setActive((index) => Math.min(index + 1, Math.max(matches.length - 1, 0)));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((index) => Math.max(index - 1, 0));
    } else if (event.key === "Enter") {
      // Never submit the surrounding form from here.
      event.preventDefault();
      if (open) pick(matches[active]);
    } else if (event.key === "Escape" && open) {
      event.stopPropagation(); // close the list, not the modal
      setOpen(false);
    }
  };

  const optionId = (index: number) => `${listId}-opt-${index}`;
  const showList = open && !loading;

  return (
    <div className="product-picker">
      <input
        role="combobox"
        aria-labelledby={labelledBy}
        aria-expanded={showList}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={showList && matches.length ? optionId(active) : undefined}
        aria-invalid={invalid || undefined}
        value={query}
        placeholder={loading ? "Đang tải danh sách sản phẩm..." : "Tìm sản phẩm theo tên, mã hoặc danh mục"}
        disabled={loading}
        onChange={(event) => { setQuery(event.target.value); setActive(0); setOpen(true); }}
        onFocus={() => setOpen(true)}
        // Already focused after a pick, so focus alone would not reopen it.
        onClick={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onKeyDown={onKeyDown}
      />
      {showList && (
        <ul className="product-picker-list" id={listId} role="listbox">
          {matches.map((product, index) => {
            // A combo's stock is derived from its parts - still the number
            // it can be sold in, so it is shown and checked like any other.
            const stock = product.stock ?? null;
            const soldOut = isSoldOut(product);
            return (
              <li
                key={product.id}
                id={optionId(index)}
                role="option"
                aria-selected={index === active}
                aria-disabled={soldOut || undefined}
                className={[index === active ? "is-active" : "", soldOut ? "is-disabled" : ""].join(" ").trim() || undefined}
                // mousedown, not click: a click lands after the input's blur
                // has already closed the list.
                onMouseDown={(event) => { event.preventDefault(); pick(product); }}
                onMouseEnter={() => setActive(index)}
              >
                <span className="product-picker-name">{product.name || product.id}</span>
                <span className="product-picker-meta">
                  {formatMoney(product.price ?? null, { whenNull: "chưa có giá" })}
                  {stock != null && (
                    <span className={stock > 0 ? undefined : "is-out"}>
                      {" · "}{stock > 0 ? `còn ${stock}` : "hết hàng"}
                    </span>
                  )}
                </span>
              </li>
            );
          })}
          {matches.length === 0 && (
            <li className="product-picker-empty" role="presentation">
              {products.length === 0
                ? "Shop chưa có sản phẩm nào. Thêm sản phẩm ở mục Sản phẩm trước."
                : `Không có sản phẩm nào khớp “${query.trim()}”.`}
            </li>
          )}
        </ul>
      )}
    </div>
  );
}

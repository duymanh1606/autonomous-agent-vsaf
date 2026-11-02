import type { Order, OrderInput, Product } from "./api";

/** One picked product as the form holds it; quantity as typed. */
export type OrderLineRow = {
  product_id: string;
  name: string;
  unit_price: number | null;
  quantity: string;
};

export function rowsFromOrder(order: Order | null): OrderLineRow[] {
  return (order?.line_items ?? []).map((line) => ({
    product_id: line.product_id,
    name: line.name,
    unit_price: line.unit_price,
    quantity: String(line.quantity),
  }));
}

/** Add a product, or one more of it if it is already on the order. */
export function addProduct(rows: OrderLineRow[], product: Product): OrderLineRow[] {
  const existing = rows.find((row) => row.product_id === product.id);
  if (existing) {
    return rows.map((row) =>
      row === existing ? { ...row, quantity: String((Number(row.quantity) || 0) + 1) } : row,
    );
  }
  return [...rows, {
    product_id: product.id,
    name: product.name || product.id,
    unit_price: product.price ?? null,
    quantity: "1",
  }];
}

function validQuantity(raw: string): boolean {
  const value = Number(raw);
  return raw.trim() !== "" && Number.isInteger(value) && value > 0;
}

/** Sum of unit price x quantity, or null when there is nothing to add up. */
export function computeTotal(rows: OrderLineRow[]): number | null {
  let total = 0;
  let counted = false;
  for (const row of rows) {
    if (row.unit_price == null || !validQuantity(row.quantity)) continue;
    total += row.unit_price * Number(row.quantity);
    counted = true;
  }
  return counted ? total : null;
}

/**
 * What the page sends. Picked products go as `{product_id, quantity}` only -
 * the server looks up each name and price. An order with no picked products
 * (one logged before the picker) keeps its typed `items`.
 */
export function orderPayload(
  form: OrderInput, rows: OrderLineRow[], legacyItems: string,
): OrderInput {
  return {
    ...form,
    line_items: rows.map((row) => ({ product_id: row.product_id, quantity: Number(row.quantity) })),
    items: rows.length ? "" : legacyItems,
  };
}

/** Order statuses that hold their products out of stock (as the server's
 * HOLDS_STOCK); cancelled and returned orders have given them back. */
export const HOLDS_STOCK = new Set(["pending", "packing", "shipping", "done"]);

/**
 * Lines asking for more than the shop has, as product id -> units available.
 * The same rule as the server (_check_stock in dashboard_api/routes/orders.py):
 * a line already on the order is only checked once its quantity goes up, and
 * a product no longer in the catalogue is not checked. No stock recorded
 * counts as none left. `held` is what this order already took out of stock
 * - an edit only needs the difference, so that is available to it too.
 */
export function overStock(
  rows: OrderLineRow[],
  stockById: Record<string, number | null | undefined>,
  originalQty: Record<string, number> = {},
  held: Record<string, number> = {},
): Record<string, number> {
  const short: Record<string, number> = {};
  for (const row of rows) {
    const quantity = Number(row.quantity);
    if (!(quantity > (originalQty[row.product_id] ?? 0))) continue;
    if (!(row.product_id in stockById)) continue;
    const available = (stockById[row.product_id] ?? 0) + (held[row.product_id] ?? 0);
    if (quantity > available) short[row.product_id] = available;
  }
  return short;
}

/**
 * What an order form still lacks before it may be saved - the same rule the
 * server enforces in dashboard_api/routes/orders.py (_validate_order and
 * _line_items), shown here so the operator sees what is missing instead of a
 * refused save. The note is optional; everything else is needed to pack and
 * ship.
 */
export function missingOrderFields(form: OrderInput): string[] {
  const missing: string[] = [];
  if (!form.customer_name.trim()) missing.push("Tên khách hàng");
  if (form.total_amount == null || !Number.isFinite(form.total_amount)) {
    missing.push("Tổng tiền");
  } else if (form.total_amount <= 0) {
    missing.push("Tổng tiền lớn hơn 0");
  }
  const lines = form.line_items ?? [];
  if (!lines.length && !(form.items ?? "").trim()) {
    missing.push("Sản phẩm");
  } else if (lines.some((line) => !Number.isInteger(line.quantity) || line.quantity <= 0)) {
    missing.push("Số lượng hợp lệ cho mỗi sản phẩm");
  }
  if (!(form.shipping_address ?? "").trim()) missing.push("Địa chỉ giao hàng");
  return missing;
}

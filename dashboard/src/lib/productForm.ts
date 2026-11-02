import type { ItemType } from "./api";

/** The product form fields the required-field rule looks at, as typed. */
export type RequiredProductFields = {
  itemType: ItemType;
  name: string;
  category: string;
  price: string;
  stock: string;
};

/**
 * What a product form still lacks before it may be saved - the same rule the
 * server enforces in dashboard_api/routes/products.py (_validate_required),
 * shown here so the operator sees what is missing instead of a refused save.
 * The agent quotes the price and answers stock questions from these, so a
 * product without them is one it cannot sell. A combo's stock is derived
 * from its components and is not asked for. Stock may be 0.
 */
export function missingProductFields(form: RequiredProductFields): string[] {
  const missing: string[] = [];
  if (!form.name.trim()) missing.push("Tên sản phẩm");
  if (!form.category.trim()) missing.push("Danh mục");
  const price = form.price.trim();
  if (!price) missing.push("Đơn giá");
  else if (!(Number(price) > 0)) missing.push("Đơn giá lớn hơn 0");
  if (form.itemType !== "combo" && !form.stock.trim()) missing.push("Tồn kho");
  return missing;
}

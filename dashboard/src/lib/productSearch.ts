import type { Product } from "./api";

/** Lower-case, accents stripped, đ as d: "Sữa Ông Thọ" matches "sua ong tho". */
export function foldVietnamese(text: string): string {
  return text
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/đ/g, "d")
    .replace(/Đ/g, "D")
    .toLowerCase()
    .trim();
}

/**
 * Products matching `query`, best first: every word of the query must appear
 * in the name, code or category; a name that starts with the query ranks
 * above one that merely contains it. Inactive products are left out - they
 * are not for sale. An empty query lists the catalogue by name.
 */
export function searchProducts(products: Product[], query: string, limit = 8): Product[] {
  const words = foldVietnamese(query).split(/\s+/).filter(Boolean);
  const scored: { product: Product; score: number }[] = [];
  for (const product of products) {
    if (product.is_active === false) continue;
    const name = foldVietnamese(product.name || product.id);
    const haystack = `${name} ${foldVietnamese(product.id)} ${foldVietnamese(product.category ?? "")}`;
    if (!words.every((word) => haystack.includes(word))) continue;
    const joined = words.join(" ");
    const score = !joined ? 0 : name.startsWith(joined) ? 0 : name.includes(joined) ? 1 : 2;
    scored.push({ product, score });
  }
  scored.sort((a, b) => a.score - b.score || a.product.name.localeCompare(b.product.name, "vi"));
  return scored.slice(0, limit).map((entry) => entry.product);
}

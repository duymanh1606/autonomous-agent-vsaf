import { describe, expect, it } from "vitest";
import type { Product } from "./api";
import { foldVietnamese, searchProducts } from "./productSearch";

const CATALOGUE = [
  { id: "sua-ong-tho", name: "Sữa Ông Thọ", category: "Sữa" },
  { id: "milo", name: "Milo", category: "Đồ uống" },
  { id: "sua-tam", name: "Sữa tắm Dove", category: "Chăm sóc" },
  { id: "cu", name: "Hàng ngừng bán", is_active: false },
] as Product[];

const names = (list: Product[]) => list.map((product) => product.name);

describe("searchProducts", () => {
  it("matches without accents, including đ", () => {
    expect(foldVietnamese("Đồ uống")).toBe("do uong");
    expect(names(searchProducts(CATALOGUE, "sua ong tho"))).toEqual(["Sữa Ông Thọ"]);
    expect(names(searchProducts(CATALOGUE, "do uong"))).toEqual(["Milo"]);
  });

  it("ranks names that start with the query first", () => {
    expect(names(searchProducts(CATALOGUE, "sua"))).toEqual(["Sữa Ông Thọ", "Sữa tắm Dove"]);
  });

  it("leaves out products that are not for sale", () => {
    expect(names(searchProducts(CATALOGUE, ""))).not.toContain("Hàng ngừng bán");
  });
});

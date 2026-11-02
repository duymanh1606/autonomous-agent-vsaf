import { describe, expect, it } from "vitest";
import { missingProductFields, type RequiredProductFields } from "./productForm";

const COMPLETE: RequiredProductFields = {
  itemType: "single", name: "Sữa tắm", category: "Tắm", price: "90000", stock: "10",
};

describe("missingProductFields", () => {
  it("accepts a complete single, stock 0 included", () => {
    expect(missingProductFields(COMPLETE)).toEqual([]);
    expect(missingProductFields({ ...COMPLETE, stock: "0" })).toEqual([]);
  });

  it("lists everything missing at once, as the tester's report had it", () => {
    // Only the name filled in.
    expect(missingProductFields({ ...COMPLETE, category: "", price: "", stock: "" })).toEqual([
      "Danh mục", "Đơn giá", "Tồn kho",
    ]);
  });

  it("requires a price above zero", () => {
    expect(missingProductFields({ ...COMPLETE, price: "0" })).toEqual(["Đơn giá lớn hơn 0"]);
  });

  it("does not ask a combo for stock", () => {
    expect(missingProductFields({ ...COMPLETE, itemType: "combo", stock: "" })).toEqual([]);
  });
});

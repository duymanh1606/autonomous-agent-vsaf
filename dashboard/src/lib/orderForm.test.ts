import { describe, expect, it } from "vitest";
import type { OrderInput, Product } from "./api";
import { addProduct, computeTotal, missingOrderFields, orderPayload, overStock, type OrderLineRow } from "./orderForm";

const MILO = { id: "milo", name: "Milo", price: 15000 } as Product;
const SUA = { id: "sua", name: "Sữa Ông Thọ", price: 25000 } as Product;

const COMPLETE: OrderInput = {
  customer_name: "Chị Lan", total_amount: 560000, status: "pending",
  shipping_address: "12 Lê Lợi, Q1", note: "",
  line_items: [{ product_id: "milo", quantity: 2 }],
};

describe("addProduct and computeTotal", () => {
  it("adds a product once and counts up when it is picked again", () => {
    let rows: OrderLineRow[] = addProduct([], MILO);
    rows = addProduct(rows, SUA);
    rows = addProduct(rows, MILO);
    expect(rows.map((row) => [row.product_id, row.quantity])).toEqual([["milo", "2"], ["sua", "1"]]);
    expect(computeTotal(rows)).toBe(2 * 15000 + 25000);
  });

  it("has no total until something can be added up", () => {
    expect(computeTotal([])).toBeNull();
    expect(computeTotal(addProduct([], { id: "x", name: "Chưa có giá" } as Product))).toBeNull();
  });
});

describe("orderPayload", () => {
  it("sends picked products as code and quantity only", () => {
    const rows = addProduct([], MILO);
    expect(orderPayload(COMPLETE, rows, "").line_items).toEqual([{ product_id: "milo", quantity: 1 }]);
  });

  it("keeps an older order's typed items until products replace them", () => {
    expect(orderPayload(COMPLETE, [], "2x Milo (ghi tay)").items).toBe("2x Milo (ghi tay)");
    expect(orderPayload(COMPLETE, addProduct([], MILO), "2x Milo (ghi tay)").items).toBe("");
  });
});

describe("missingOrderFields", () => {
  it("accepts a complete order without a note", () => {
    expect(missingOrderFields(COMPLETE)).toEqual([]);
  });

  it("lists everything missing at once", () => {
    expect(missingOrderFields({ ...COMPLETE, line_items: [], shipping_address: "" })).toEqual([
      "Sản phẩm", "Địa chỉ giao hàng",
    ]);
  });

  it("accepts an older order that has only typed items", () => {
    expect(missingOrderFields({ ...COMPLETE, line_items: [], items: "2x Milo" })).toEqual([]);
  });

  it("wants a whole quantity above zero on every line", () => {
    expect(missingOrderFields({ ...COMPLETE, line_items: [{ product_id: "milo", quantity: 0 }] }))
      .toEqual(["Số lượng hợp lệ cho mỗi sản phẩm"]);
  });

  it("requires a total above zero", () => {
    expect(missingOrderFields({ ...COMPLETE, total_amount: null })).toEqual(["Tổng tiền"]);
    expect(missingOrderFields({ ...COMPLETE, total_amount: 0 })).toEqual(["Tổng tiền lớn hơn 0"]);
  });
});

describe("overStock", () => {
  const rows = (quantity: string): OrderLineRow[] => [
    { product_id: "milo", name: "Milo", unit_price: 15000, quantity },
  ];

  it("flags a line above the stock, not one at it", () => {
    expect(overStock(rows("10"), { milo: 9 })).toEqual({ milo: 9 });
    expect(overStock(rows("9"), { milo: 9 })).toEqual({});
  });

  it("leaves an existing line alone until its quantity goes up", () => {
    expect(overStock(rows("8"), { milo: 2 }, { milo: 8 })).toEqual({});
    expect(overStock(rows("9"), { milo: 2 }, { milo: 8 })).toEqual({ milo: 2 });
  });

  it("counts what the order already holds when it is edited", () => {
    // 100 placed earlier (stock fell 150 -> 50): up to 150 is fine, 151 is not.
    expect(overStock(rows("150"), { milo: 50 }, { milo: 100 }, { milo: 100 })).toEqual({});
    expect(overStock(rows("151"), { milo: 50 }, { milo: 100 }, { milo: 100 })).toEqual({ milo: 150 });
  });

  it("does not judge a product that has left the catalogue; no stock is none", () => {
    expect(overStock(rows("5"), {})).toEqual({});
    expect(overStock(rows("1"), { milo: null })).toEqual({ milo: 0 });
  });
});

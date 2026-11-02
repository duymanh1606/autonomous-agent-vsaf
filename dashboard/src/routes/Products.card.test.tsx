import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Products } from "./Products";
import { api } from "../lib/api";
import { ToastProvider } from "../components/Toast";
import type { Product } from "../lib/api";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, api: { ...actual.api, products: vi.fn() } };
});

const base = {
  ingredients: [], benefits: [], warnings: [], keywords: [], tags: [],
  price: 15000, stock: 4, item_type: "single" as const, category: "Đồ uống",
};

function renderProducts(products: Product[]) {
  (api.products as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
    products, missing_media: [], invalid_product_directories: [],
  });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ToastProvider>
        <Products accountId="acc-1" />
      </ToastProvider>
    </QueryClientProvider>,
  );
}

describe("Products - thẻ sản phẩm có ảnh và mô tả", () => {
  it("shows the first image and the description", async () => {
    renderProducts([{
      ...base, id: "milo", name: "Milo", description: "Sữa lúa mạch 180ml",
      media: {
        images: [{ relative_path: "images/a.jpg", exists: true }, { relative_path: "images/b.jpg" }],
        videos: [{ relative_path: "videos/c.mp4" }],
      },
    } as Product]);

    const open = await screen.findByRole("button", { name: "Xem chi tiết Milo" });
    const img = open.querySelector("img")!;
    expect(img.getAttribute("src")).toBe("/api/accounts/acc-1/products/milo/media/images/a.jpg");
    expect(open.textContent).toContain("2 ảnh · 1 video");
    expect(screen.getByText("Sữa lúa mạch 180ml")).toBeTruthy();
  });

  it("says so when a product has no photo or description yet", async () => {
    renderProducts([{
      ...base, id: "sua", name: "Sữa", description: "", media: { images: [], videos: [] },
    } as Product]);

    const open = await screen.findByRole("button", { name: "Xem chi tiết Sữa" });
    expect(open.textContent).toContain("Chưa có ảnh");
    expect(screen.getByText("Chưa có mô tả")).toBeTruthy();
  });

  it("falls back to the placeholder when the image file cannot be loaded", async () => {
    renderProducts([{
      ...base, id: "hu", name: "Ảnh hỏng", description: "x",
      media: { images: [{ relative_path: "images/gone.jpg" }], videos: [] },
    } as Product]);

    const open = await screen.findByRole("button", { name: "Xem chi tiết Ảnh hỏng" });
    fireEvent.error(open.querySelector("img")!);
    expect(open.textContent).toContain("Không tải được ảnh");
  });

  it("marks a product at or under its threshold with a red alert", async () => {
    renderProducts([
      { ...base, id: "a", name: "Áo sắp hết", stock: 2, low_stock_threshold: 5,
        description: "x", media: { images: [], videos: [] } },
      { ...base, id: "b", name: "Áo hết", stock: 0, low_stock_threshold: null,
        description: "x", media: { images: [], videos: [] } },
      { ...base, id: "c", name: "Áo còn nhiều", stock: 40, low_stock_threshold: 5,
        description: "x", media: { images: [], videos: [] } },
    ] as Product[]);

    const low = await screen.findByText("Sắp hết hàng · còn 2 (ngưỡng 5)");
    expect(low.closest(".stock-alert")?.className).toContain("is-low");
    expect(low.closest(".product-card")?.className).toContain("has-stock-alert");
    // Out of stock is flagged even with no threshold set.
    expect(screen.getByText("Hết hàng", { selector: ".stock-alert" }).className).toContain("is-out");
    const plenty = screen.getByRole("heading", { name: "Áo còn nhiều" }).closest(".product-card")!;
    expect(plenty.className).not.toContain("has-stock-alert");
    expect(plenty.querySelector(".stock-alert")).toBeNull();
  });
});

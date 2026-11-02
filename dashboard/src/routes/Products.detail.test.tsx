import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Products } from "./Products";
import { api } from "../lib/api";
import type { Product } from "../lib/api";
import { ToastProvider } from "../components/Toast";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, api: { ...actual.api, products: vi.fn() } };
});

const LONG_DESCRIPTION =
  "Sữa rửa mặt dịu nhẹ cho da nhạy cảm, không chứa xà phòng, không gây khô căng, " +
  "phù hợp dùng hằng ngày sáng và tối, đã kiểm nghiệm da liễu và an toàn cho da mụn.";

const bare: Product = {
  id: "sp-bare", name: "Sản phẩm trống", description: "", ingredients: [], benefits: [],
  warnings: [], keywords: [], tags: [], price: null, stock: null,
  media: { images: [], videos: [] },
};

const full: Product = {
  id: "sp-full", name: "Sữa rửa mặt Cetaphil", brand: "Cetaphil", category: "Chăm sóc da",
  description: LONG_DESCRIPTION, ingredients: ["Nước", "Glycerin"], benefits: ["Dịu nhẹ", "Không khô da"],
  warnings: ["Ngưng dùng nếu kích ứng"], keywords: ["srm", "da nhạy cảm"], tags: ["skincare"],
  price: 320000, stock: 12, item_type: "single", media: { images: [], videos: [] },
};

function setup(products: Product[] = [bare, full]) {
  (api.products as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
    products, missing_media: [], invalid_product_directories: [],
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <Products accountId="acc-1" />
      </ToastProvider>
    </QueryClientProvider>,
  );
}

function cardFor(text: string): HTMLElement {
  return screen.getByText(text).closest(".product-card") as HTMLElement;
}

describe("Products - thẻ gọn + Xem chi tiết", () => {
  it("truncates the description on the card (CSS clamp) rather than showing it in full", async () => {
    setup();
    await screen.findByText("Sữa rửa mặt Cetaphil");
    const card = cardFor("Sữa rửa mặt Cetaphil");
    const desc = within(card).getByText(LONG_DESCRIPTION);
    expect(desc).toHaveClass("product-card-desc");
  });

  it("only offers 'Xem chi tiết' when there is something extra behind it", async () => {
    setup();
    await screen.findByText("Sản phẩm trống");
    const bareCard = cardFor("Sản phẩm trống");
    expect(within(bareCard).queryByRole("button", { name: "Xem chi tiết" })).not.toBeInTheDocument();

    const fullCard = cardFor("Sữa rửa mặt Cetaphil");
    expect(within(fullCard).getByRole("button", { name: "Xem chi tiết" })).toBeInTheDocument();
  });

  it("opens a read-only dialog with the full description, ingredients, benefits and warnings", async () => {
    const user = userEvent.setup();
    setup();
    await screen.findByText("Sữa rửa mặt Cetaphil");
    const card = cardFor("Sữa rửa mặt Cetaphil");
    await user.click(within(card).getByRole("button", { name: "Xem chi tiết" }));

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(LONG_DESCRIPTION)).toBeInTheDocument();
    expect(within(dialog).getByText("Nước")).toBeInTheDocument();
    expect(within(dialog).getByText("Dịu nhẹ")).toBeInTheDocument();
    expect(within(dialog).getByText(/Ngưng dùng nếu kích ứng/)).toBeInTheDocument();
    expect(within(dialog).getAllByText(/Cetaphil/).length).toBeGreaterThan(0); // brand, in the meta line
  });

  it("'Sửa sản phẩm' inside the detail dialog closes it and opens the edit form for the same product", async () => {
    const user = userEvent.setup();
    setup();
    await screen.findByText("Sữa rửa mặt Cetaphil");
    const card = cardFor("Sữa rửa mặt Cetaphil");
    await user.click(within(card).getByRole("button", { name: "Xem chi tiết" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Sửa sản phẩm" }));

    expect(screen.queryByRole("heading", { name: /Chi tiết/ })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Sửa sản phẩm" })).toBeInTheDocument();
    expect(screen.getByDisplayValue("Sữa rửa mặt Cetaphil")).toBeInTheDocument();
  });

  it("a product with only a description (no media/warnings) still gets 'Xem chi tiết'", async () => {
    const onlyDesc: Product = { ...bare, id: "sp-desc", name: "Chỉ có mô tả", description: "Ngắn thôi." };
    const user = userEvent.setup();
    setup([onlyDesc]);
    await screen.findByText("Chỉ có mô tả");
    const card = cardFor("Chỉ có mô tả");
    await user.click(within(card).getByRole("button", { name: "Xem chi tiết" }));
    expect(within(screen.getByRole("dialog")).getByText("Ngắn thôi.")).toBeInTheDocument();
  });
});

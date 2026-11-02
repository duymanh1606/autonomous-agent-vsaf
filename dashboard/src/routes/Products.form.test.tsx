import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Products } from "./Products";
import { api } from "../lib/api";
import { ToastProvider } from "../components/Toast";
import type { Product } from "../lib/api";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, api: { ...actual.api, products: vi.fn(), createProduct: vi.fn(), updateProduct: vi.fn() } };
});

function renderProducts(products: Product[] = []) {
  (api.products as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
    products, missing_media: [], invalid_product_directories: [],
  });
  const user = userEvent.setup();
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ToastProvider>
        <Products accountId="acc-1" />
      </ToastProvider>
    </QueryClientProvider>,
  );
  return user;
}

async function openCreateForm() {
  const user = renderProducts();
  await user.click(await screen.findByRole("button", { name: "+ Thêm sản phẩm" }));
  await user.type(screen.getByLabelText("Tên sản phẩm"), "Áo hoodie");
  return user;
}

describe("Products - ô số chỉ nhận số", () => {
  beforeEach(() => vi.clearAllMocks());

  it("drops letters and the minus sign as they are typed", async () => {
    const user = await openCreateForm();
    const stock = screen.getByLabelText("Tồn kho") as HTMLInputElement;
    await user.type(stock, "-8abc");
    expect(stock.value).toBe("8");

    // Whole numbers only for a count; a price may carry decimals.
    await user.type(stock, ".5");
    expect(stock.value).toBe("85");
    const price = screen.getByLabelText("Đơn giá") as HTMLInputElement;
    await user.type(price, "-12,5x");
    expect(price.value).toBe("12.5");
    expect(screen.queryByText("Không được nhỏ hơn 0, vui lòng nhập lại.")).toBeNull();
  });

  it("drops letters from the combo quantity too", async () => {
    const user = await openCreateForm();
    await user.selectOptions(screen.getByLabelText(/^Loại sản phẩm/), "combo");
    const quantity = screen.getByLabelText("Số lượng trong 1 combo") as HTMLInputElement;
    await user.type(quantity, "aa2");
    expect(quantity.value).toBe("2");
  });
});

const fieldLabels = () =>
  Array.from(document.querySelectorAll(".product-form-field > span")).map((el) => el.textContent);

describe("Products - form thu gọn", () => {
  beforeEach(() => vi.clearAllMocks());

  it("asks only for the fields the shop fills in, and no product code", async () => {
    await openCreateForm();
    expect(fieldLabels()).toEqual([
      "Loại sản phẩm", "Tên sản phẩm", "Danh mục", "Đơn giá", "Giá vốn",
      "Đơn vị tiền tệ", "Tồn kho", "Ngưỡng cảnh báo sắp hết", "Đang sử dụng", "Mô tả",
    ]);
    expect(screen.getByRole("button", { name: "+ Tải file lên" })).toBeTruthy();
  });

  it("will not save with only a name, and says what is missing", async () => {
    // The tester's report: name filled, everything else left blank.
    await openCreateForm();
    expect((screen.getByRole("button", { name: "Tạo sản phẩm" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole("status").textContent).toBe("Còn thiếu: Danh mục, Đơn giá, Tồn kho");
    expect(screen.getByLabelText("Đơn giá").getAttribute("aria-required")).toBe("true");
  });

  it("creates the product under a random code", async () => {
    (api.createProduct as ReturnType<typeof vi.fn>).mockResolvedValue({ id: "x" });
    const user = await openCreateForm();
    await user.type(screen.getByLabelText("Danh mục"), "Thời trang");
    await user.type(screen.getByLabelText("Đơn giá"), "350000");
    await user.type(screen.getByLabelText("Tồn kho"), "0");
    await user.click(screen.getByRole("button", { name: "Tạo sản phẩm" }));

    await vi.waitFor(() => expect(api.createProduct).toHaveBeenCalled());
    const [, productId, fields] = (api.createProduct as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(productId).toMatch(/^sp-[0-9a-f]{8}$/);
    expect(fields.name).toBe("Áo hoodie");
  });

  it("keeps what an existing product had, shown or not", async () => {
    (api.updateProduct as ReturnType<typeof vi.fn>).mockResolvedValue({});
    const user = renderProducts([{
      id: "sp007", name: "Sữa rửa mặt", brand: "Cetaphil", description: "", category: "Chăm sóc da",
      ingredients: ["Nước"], benefits: [], warnings: [], keywords: ["srm"], tags: [],
      price: 100, stock: 3, low_stock_threshold: 2, is_active: false, item_type: "single",
      media: { images: [], videos: [] },
    }]);
    await user.click(await screen.findByRole("button", { name: "Sửa" }));
    // Both still on the form, showing what the product has.
    expect((screen.getByLabelText(/^Ngưỡng cảnh báo sắp hết/) as HTMLInputElement).value).toBe("2");
    expect((screen.getByLabelText(/^Đang sử dụng/) as HTMLInputElement).checked).toBe(false);
    await user.click(screen.getByRole("button", { name: "Lưu thay đổi" }));

    await vi.waitFor(() => expect(api.updateProduct).toHaveBeenCalled());
    const [, productId, fields] = (api.updateProduct as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(productId).toBe("sp007");
    expect(fields).toMatchObject({
      brand: "Cetaphil", ingredients: ["Nước"], keywords: ["srm"],
      low_stock_threshold: 2, is_active: false,
    });
  });
});

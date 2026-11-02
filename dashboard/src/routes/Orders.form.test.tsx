import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Orders } from "./Orders";
import { api } from "../lib/api";
import { ToastProvider } from "../components/Toast";
import type { Order, Product } from "../lib/api";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api, orders: vi.fn(), products: vi.fn(), createOrder: vi.fn(), updateOrder: vi.fn(),
      conversations: vi.fn(),
    },
  };
});

const CATALOGUE = [
  { id: "milo", name: "Milo", price: 15000, stock: 9, category: "Đồ uống" },
  { id: "sua", name: "Sữa Ông Thọ", price: 25000, stock: 3, category: "Sữa" },
  { id: "het", name: "Sữa chua hết hàng", price: 8000, stock: 0, category: "Sữa" },
] as Product[];

function renderOrders(orders: Order[] = []) {
  (api.orders as ReturnType<typeof vi.fn>).mockResolvedValue(orders);
  (api.conversations as ReturnType<typeof vi.fn>).mockResolvedValue([
    { conversation_id: "0:1:111:999", display_name: "Chị Lan", username: "lan.shop" },
    { conversation_id: "0:1:222:999", display_name: null, username: "binh99" },
  ]);
  (api.products as ReturnType<typeof vi.fn>).mockResolvedValue({
    products: CATALOGUE, missing_media: [], invalid_product_directories: [],
  });
  const user = userEvent.setup();
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ToastProvider>
        <Orders accountId="acc-1" />
      </ToastProvider>
    </QueryClientProvider>,
  );
  return user;
}

const total = () => screen.getByLabelText("Tổng tiền (đ)") as HTMLInputElement;

async function openForm() {
  const user = renderOrders();
  await user.click(await screen.findByRole("button", { name: "+ Ghi đơn mới" }));
  const picker = await screen.findByRole("combobox", { name: "Sản phẩm" });
  await vi.waitFor(() => expect((picker as HTMLInputElement).disabled).toBe(false));
  return { user, picker };
}

describe("Orders - chọn sản phẩm từ danh mục", () => {
  beforeEach(() => vi.clearAllMocks());

  it("closes the list once a product is picked, by mouse or keyboard", async () => {
    const { user, picker } = await openForm();
    await user.click(picker);
    expect(screen.getByRole("listbox")).toBeTruthy();
    await user.click(screen.getByRole("option", { name: /Milo/ }));
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(picker.getAttribute("aria-expanded")).toBe("false");

    await user.type(picker, "sua ong{Enter}");
    expect(screen.queryByRole("listbox")).toBeNull();
    // Clicking the box again brings the list back for the next product.
    await user.click(picker);
    expect(screen.getByRole("listbox")).toBeTruthy();
  });

  it("lists a sold-out product but will not add it", async () => {
    const { user, picker } = await openForm();
    await user.type(picker, "het hang");
    const option = screen.getByRole("option", { name: /Sữa chua hết hàng.*hết hàng/ });
    expect(option.getAttribute("aria-disabled")).toBe("true");
    await user.click(option);
    await user.keyboard("{Enter}");
    expect(screen.queryByLabelText("Số lượng Sữa chua hết hàng")).toBeNull();
  });

  it("will not save more than the stock, and says how many are left", async () => {
    const { user, picker } = await openForm();
    await user.type(screen.getByLabelText("Tên khách hàng"), "Chị Lan");
    await user.type(screen.getByLabelText("Địa chỉ giao hàng"), "12 Lê Lợi");
    await user.type(picker, "sua ong{Enter}");
    const quantity = screen.getByLabelText("Số lượng Sữa Ông Thọ");
    await user.clear(quantity);
    await user.type(quantity, "4");

    expect(screen.getByRole("alert").textContent).toBe("Chỉ còn 3 trong kho.");
    expect(quantity.getAttribute("aria-invalid")).toBe("true");
    expect((screen.getByRole("button", { name: "Tạo đơn hàng" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole("status").textContent).toContain("Số lượng không vượt tồn kho");

    await user.clear(quantity);
    await user.type(quantity, "3");
    expect(screen.queryByRole("alert")).toBeNull();
    expect((screen.getByRole("button", { name: "Tạo đơn hàng" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("picks products by searching, and adds up the total", async () => {
    (api.createOrder as ReturnType<typeof vi.fn>).mockResolvedValue({});
    const user = renderOrders();
    await user.click(await screen.findByRole("button", { name: "+ Ghi đơn mới" }));
    await user.type(screen.getByLabelText("Tên khách hàng"), "Chị Lan");

    const picker = await screen.findByRole("combobox", { name: "Sản phẩm" });
    await vi.waitFor(() => expect((picker as HTMLInputElement).disabled).toBe(false));
    await user.type(picker, "sua ong");
    await user.keyboard("{Enter}");
    await user.type(picker, "milo{Enter}");
    await user.type(picker, "milo{Enter}");

    expect(total().value).toBe(String(25000 + 2 * 15000));
    expect((screen.getByLabelText("Số lượng Milo") as HTMLInputElement).value).toBe("2");

    await user.type(screen.getByLabelText("Địa chỉ giao hàng"), "12 Lê Lợi");
    await user.click(screen.getByRole("button", { name: "Tạo đơn hàng" }));
    await vi.waitFor(() => expect(api.createOrder).toHaveBeenCalled());
    const [, sent] = (api.createOrder as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(sent.line_items).toEqual([
      { product_id: "sua", quantity: 1 }, { product_id: "milo", quantity: 2 },
    ]);
    expect(sent.total_amount).toBe(55000);
  });

  it("stops following the products once the total is typed by hand", async () => {
    const user = renderOrders();
    await user.click(await screen.findByRole("button", { name: "+ Ghi đơn mới" }));
    const picker = await screen.findByRole("combobox", { name: "Sản phẩm" });
    await vi.waitFor(() => expect((picker as HTMLInputElement).disabled).toBe(false));
    await user.type(picker, "milo{Enter}");
    await user.clear(total());
    await user.type(total(), "10000");

    await user.type(picker, "milo{Enter}");
    expect(total().value).toBe("10000");
    await user.click(screen.getByRole("button", { name: "Dùng số này" }));
    expect(total().value).toBe("30000");
  });

  it("shows an older order's typed items and keeps them if nothing is picked", async () => {
    (api.updateOrder as ReturnType<typeof vi.fn>).mockResolvedValue({});
    const user = renderOrders([{
      id: 7, account_id: "acc-1", customer_name: "Anh Bình", conversation_id: null,
      items: "2x Milo (ghi tay)", line_items: [], stock_deducted: false, total_amount: 30000, status: "pending",
      shipping_address: "5 Hai Bà Trưng", note: "", created_at: "2026-09-01T00:00:00Z",
      updated_at: "2026-09-01T00:00:00Z",
    }]);
    await user.click(await screen.findByRole("button", { name: /Sửa/ }));
    expect(screen.getByText(/Ghi tay trước đây: 2x Milo \(ghi tay\)/)).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Lưu thay đổi" }));

    await vi.waitFor(() => expect(api.updateOrder).toHaveBeenCalled());
    const [, , sent] = (api.updateOrder as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(sent.items).toBe("2x Milo (ghi tay)");
    expect(sent.line_items).toEqual([]);
  });

  it("links the order to a conversation, so it gets a row on the Google Sheet", async () => {
    (api.createOrder as ReturnType<typeof vi.fn>).mockResolvedValue({});
    const user = renderOrders();
    await user.click(await screen.findByRole("button", { name: "+ Ghi đơn mới" }));
    const select = screen.getByLabelText("Hội thoại TikTok");
    await vi.waitFor(() => expect(screen.getByRole("option", { name: "Chị Lan (@lan.shop)" })).toBeTruthy());
    await user.selectOptions(select, "0:1:111:999");
    // An empty name is filled from the conversation.
    expect((screen.getByLabelText("Tên khách hàng") as HTMLInputElement).value).toBe("Chị Lan");

    const picker = await screen.findByRole("combobox", { name: "Sản phẩm" });
    await vi.waitFor(() => expect((picker as HTMLInputElement).disabled).toBe(false));
    await user.type(picker, "milo{Enter}");
    await user.type(screen.getByLabelText("Địa chỉ giao hàng"), "12 Lê Lợi");
    await user.click(screen.getByRole("button", { name: "Tạo đơn hàng" }));
    await vi.waitFor(() => expect(api.createOrder).toHaveBeenCalled());
    const [, sent] = (api.createOrder as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(sent.conversation_id).toBe("0:1:111:999");
  });

  it("keeps an order's conversation when it is edited", async () => {
    (api.updateOrder as ReturnType<typeof vi.fn>).mockResolvedValue({});
    const user = renderOrders([{
      id: 8, account_id: "acc-1", customer_name: "Anh Bình", conversation_id: "0:1:222:999",
      items: "1x Milo", line_items: [], stock_deducted: false, total_amount: 15000, status: "pending",
      shipping_address: "5 Hai Bà Trưng", note: "", created_at: "2026-09-01T00:00:00Z",
      updated_at: "2026-09-01T00:00:00Z",
    }]);
    await user.click(await screen.findByRole("button", { name: /Sửa/ }));
    await user.click(screen.getByRole("button", { name: "Lưu thay đổi" }));
    await vi.waitFor(() => expect(api.updateOrder).toHaveBeenCalled());
    const [, , sent] = (api.updateOrder as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(sent.conversation_id).toBe("0:1:222:999");
  });
});

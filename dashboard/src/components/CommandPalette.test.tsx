import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CommandPalette } from "./CommandPalette";
import { api } from "../lib/api";
import type { Account, SearchResults } from "../lib/api";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, api: { ...actual.api, search: vi.fn() } };
});

const search = api.search as unknown as ReturnType<typeof vi.fn>;

const ws = (id: string, label: string) =>
  ({ id, label, owner_id: null } as unknown as Account);

const results: SearchResults = {
  customers: [
    { conversation_id: "conv-1", display_name: "Nguyễn Đức Ánh", username: "anh.nd", customer_id: "u1",
      phone: "0912", last_message_at: null, matched: "identity", snippet: null },
    { conversation_id: "conv-2", display_name: "Khách B", username: null, customer_id: null,
      phone: null, last_message_at: null, matched: "message", snippet: "…giao ở Đà Nẵng…" },
  ],
  orders: [{ id: 7, customer_name: "Lê Hoa", status: "shipping", total_amount: 250000, items: "Áo x2", created_at: null }],
  products: [{ id: "sp001", name: "Sữa rửa mặt", price: 120000, stock: 3, is_active: true }],
};

function renderPalette(overrides: Partial<Parameters<typeof CommandPalette>[0]> = {}) {
  const props = {
    open: true, onClose: vi.fn(), accountId: "ws-a",
    accounts: [ws("ws-a", "Shop A"), ws("ws-b", "Shop Đồ Chơi")],
    onNavigate: vi.fn(), onSelectWorkspace: vi.fn(), onOpenConversation: vi.fn(),
    onOpenOrder: vi.fn(), onOpenProduct: vi.fn(), ...overrides,
  };
  return { ...render(<CommandPalette {...props} />), props };
}

describe("CommandPalette", () => {
  beforeEach(() => {
    search.mockReset();
    search.mockResolvedValue(results);
  });

  it("shows every navigation command before anything is typed, and does not search", () => {
    renderPalette();
    expect(screen.getByText("Mở Tin nhắn")).toBeInTheDocument();
    expect(screen.getByText("Mở Đơn hàng")).toBeInTheDocument();
    expect(search).not.toHaveBeenCalled();
  });

  it("does not hit the server for a single character", async () => {
    const user = userEvent.setup();
    renderPalette();
    await user.type(screen.getByRole("textbox"), "n");
    await new Promise((r) => setTimeout(r, 250));
    expect(search).not.toHaveBeenCalled();
  });

  it("renders customers (with a message snippet), orders and products from the server", async () => {
    const user = userEvent.setup();
    renderPalette();
    await user.type(screen.getByRole("textbox"), "nguyen");
    await waitFor(() => expect(search).toHaveBeenCalledWith("ws-a", "nguyen", expect.anything()));

    expect(await screen.findByText("Nguyễn Đức Ánh")).toBeInTheDocument();
    expect(screen.getByText("@anh.nd · 0912")).toBeInTheDocument();
    expect(screen.getByText("…giao ở Đà Nẵng…")).toBeInTheDocument();
    expect(screen.getByText("#7 · Lê Hoa")).toBeInTheDocument();
    expect(screen.getByText("Sữa rửa mặt")).toBeInTheDocument();
    for (const group of ["Khách hàng", "Đơn hàng", "Sản phẩm"]) {
      expect(screen.getByText(group)).toBeInTheDocument();
    }
  });

  it("matches navigation and other workspaces without diacritics", async () => {
    const user = userEvent.setup();
    search.mockResolvedValue({ customers: [], orders: [], products: [] });
    renderPalette();
    await user.type(screen.getByRole("textbox"), "do choi");
    expect(await screen.findByText("Shop Đồ Chơi")).toBeInTheDocument();
    expect(screen.queryByText("Shop A")).not.toBeInTheDocument(); // the current one is not offered

    await user.clear(screen.getByRole("textbox"));
    await user.type(screen.getByRole("textbox"), "don hang");
    expect(await screen.findByText("Mở Đơn hàng")).toBeInTheDocument();
    expect(screen.queryByText("Mở Tin nhắn")).not.toBeInTheDocument();
  });

  it("walks results with the arrow keys and opens the highlighted one on Enter", async () => {
    const user = userEvent.setup();
    const { props } = renderPalette();
    const input = screen.getByRole("textbox");
    await user.type(input, "nguyen");
    await screen.findByText("Nguyễn Đức Ánh");

    // Order of the flat list: 2 customers, 1 order, 1 product, then pages.
    await user.keyboard("{ArrowDown}{ArrowDown}");
    expect(screen.getByRole("option", { selected: true })).toHaveTextContent("#7 · Lê Hoa");
    await user.keyboard("{ArrowUp}");
    expect(screen.getByRole("option", { selected: true })).toHaveTextContent("Khách B");
    await user.keyboard("{Enter}");

    expect(props.onOpenConversation).toHaveBeenCalledWith("conv-2");
    expect(props.onClose).toHaveBeenCalled();
  });

  it("clicking an order or product deep-links and closes", async () => {
    const user = userEvent.setup();
    const { props } = renderPalette();
    await user.type(screen.getByRole("textbox"), "hoa");
    await user.click(await screen.findByText("#7 · Lê Hoa"));
    expect(props.onOpenOrder).toHaveBeenCalledWith(7);

    await user.click(screen.getByText("Sữa rửa mặt"));
    expect(props.onOpenProduct).toHaveBeenCalledWith("sp001");
    expect(props.onClose).toHaveBeenCalledTimes(2);
  });

  it("says so when nothing matches", async () => {
    const user = userEvent.setup();
    search.mockResolvedValue({ customers: [], orders: [], products: [] });
    renderPalette();
    await user.type(screen.getByRole("textbox"), "zzzz");
    expect(await screen.findByText('Không tìm thấy gì cho "zzzz"')).toBeInTheDocument();
  });

  it("explains that a workspace is needed for data search when none is selected", async () => {
    const user = userEvent.setup();
    renderPalette({ accountId: null });
    await user.type(screen.getByRole("textbox"), "nguyen");
    expect(await screen.findByText(/Chọn một workspace/)).toBeInTheDocument();
    expect(search).not.toHaveBeenCalled();
  });

  it("aborts the earlier request when the query changes", async () => {
    const user = userEvent.setup();
    const signals: AbortSignal[] = [];
    search.mockImplementation((_id: string, _q: string, signal: AbortSignal) => {
      signals.push(signal);
      return new Promise(() => {}); // never resolves - like a slow server
    });
    renderPalette();
    const input = screen.getByRole("textbox");
    await user.type(input, "ng");
    await waitFor(() => expect(signals).toHaveLength(1));
    await user.type(input, "uyen");
    await waitFor(() => expect(signals.length).toBeGreaterThanOrEqual(2));
    expect(signals[0].aborted).toBe(true);
  });

  it("resets the query when reopened", async () => {
    const user = userEvent.setup();
    const { rerender, props } = renderPalette();
    await user.type(screen.getByRole("textbox"), "abc");
    rerender(<CommandPalette {...props} open={false} />);
    await act(async () => { rerender(<CommandPalette {...props} open={true} />); });
    expect(screen.getByRole("textbox")).toHaveValue("");
  });
});

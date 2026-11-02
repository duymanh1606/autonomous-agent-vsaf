import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Customers } from "./Customers";
import { api } from "../lib/api";
import type { Conversation } from "../lib/api";
import { ToastProvider } from "../components/Toast";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, api: { ...actual.api, conversations: vi.fn(), conversation: vi.fn() } };
});

const AUTO: Conversation["control"] = {
  mode: "AUTO", operator_id: null, takeover_source: null, reason: null,
};

const conv = (id: string, name: string, stage: string | null): Conversation => ({
  conversation_id: id, last_message_at: "2026-09-01T10:00:00Z", message_count: 1,
  inbound: 1, outbound: 0, username: null, display_name: name, avatar_url: null,
  customer_id: id, intent: null, stage, requires_human: false, summary: null, control: AUTO,
});

const conversations = [
  conv("c1", "Khách A", "new"),
  conv("c2", "Khách B", "ordering"),
  conv("c3", "Khách C", "ordering"),
  conv("c4", "Khách D", null),
];

function setup(rows: Conversation[] = conversations) {
  (api.conversations as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(rows);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <Customers accountId="acc-1" />
      </ToastProvider>
    </QueryClientProvider>,
  );
}

function stageSelect(): HTMLSelectElement {
  return screen.getByLabelText("Lọc theo phân loại khách hàng") as HTMLSelectElement;
}

describe("Inbox - lọc theo phân loại khách hàng", () => {
  it("offers only the stages actually present, plus 'Chưa phân loại' when some conversations lack one", async () => {
    setup();
    await screen.findByText("Khách A");

    const select = stageSelect();
    const options = within(select).getAllByRole("option").map((o) => o.textContent);
    expect(options).toEqual(["Tất cả phân loại", "Khách mới", "Đang đặt hàng", "Chưa phân loại"]);
  });

  it("does not offer 'Chưa phân loại' when every conversation already has a stage", async () => {
    setup(conversations.filter((c) => c.stage));
    await screen.findByText("Khách A");

    const options = within(stageSelect()).getAllByRole("option").map((o) => o.textContent);
    expect(options).not.toContain("Chưa phân loại");
  });

  it("filters the list down to conversations in the chosen stage", async () => {
    const user = userEvent.setup();
    setup();
    await screen.findByText("Khách A");

    await user.selectOptions(stageSelect(), "Đang đặt hàng");

    expect(screen.getByText("Khách B")).toBeInTheDocument();
    expect(screen.getByText("Khách C")).toBeInTheDocument();
    expect(screen.queryByText("Khách A")).not.toBeInTheDocument();
    expect(screen.queryByText("Khách D")).not.toBeInTheDocument();
  });

  it("filters down to conversations with no classification yet", async () => {
    const user = userEvent.setup();
    setup();
    await screen.findByText("Khách A");

    await user.selectOptions(stageSelect(), "Chưa phân loại");

    expect(screen.getByText("Khách D")).toBeInTheDocument();
    expect(screen.queryByText("Khách A")).not.toBeInTheDocument();
    expect(screen.queryByText("Khách B")).not.toBeInTheDocument();

    await user.selectOptions(stageSelect(), "Tất cả phân loại");
    expect(screen.getByText("Khách A")).toBeInTheDocument();
    expect(screen.getByText("Khách D")).toBeInTheDocument();
  });

  it("combines with the search box and the status filter (both must match)", async () => {
    const user = userEvent.setup();
    setup();
    await screen.findByText("Khách A");

    await user.selectOptions(stageSelect(), "Đang đặt hàng");
    await user.type(screen.getByPlaceholderText("🔍 Tìm kiếm khách hàng..."), "Khách C");

    expect(screen.getByText("Khách C")).toBeInTheDocument();
    expect(screen.queryByText("Khách B")).not.toBeInTheDocument();
  });

  it("shows the empty-state message when the stage filter matches nothing", async () => {
    const user = userEvent.setup();
    setup(conversations.filter((c) => c.stage !== "complaint")); // none have "complaint"
    await screen.findByText("Khách A");

    // Hand-craft a mismatch: search for a stage no row has.
    await user.type(screen.getByPlaceholderText("🔍 Tìm kiếm khách hàng..."), "khong ton tai");
    expect(await screen.findByText("Không tìm thấy khách hàng phù hợp")).toBeInTheDocument();
  });
});

import { render, screen, within, act, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Customers } from "./Customers";
import { api } from "../lib/api";
import type { Conversation, ConversationDetail } from "../lib/api";
import { ToastProvider } from "../components/Toast";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, conversations: vi.fn(), conversation: vi.fn(), sendConversationMessage: vi.fn() },
  };
});

const HUMAN: Conversation["control"] = {
  mode: "HUMAN", operator_id: "op", takeover_source: "MANUAL", reason: null,
};
const conversation: Conversation = {
  conversation_id: "c1", last_message_at: "2026-09-01T10:00:00Z", message_count: 1, inbound: 1,
  outbound: 0, username: null, display_name: "Khách A", avatar_url: null, customer_id: "c1",
  intent: null, stage: "new", requires_human: false, summary: null, control: HUMAN,
};
type Msg = ConversationDetail["messages"][number];
const inbound = (from: number, to: number): Msg[] =>
  Array.from({ length: to - from }, (_, i) => ({
    message_id: `m${from + i}`, direction: "INBOUND", text: `tin ${from + i}`,
    created_at: "2026-09-01T10:00:00Z", source: "dom", media: [],
  }));
const detailOf = (messages: Msg[]): ConversationDetail =>
  ({ conversation_id: "c1", profile: { username: "user" }, facts: [], state: {}, messages, control: HUMAN });
const mocked = (name: keyof typeof api) => api[name] as unknown as ReturnType<typeof vi.fn>;

const INDICATOR = "↓ Tin nhắn mới";

// jsdom has no layout. Give the list a fixed geometry so `handleScroll` can
// see the operator as scrolled up (2000 - 0 - 400 = 1600 from the bottom).
beforeAll(() => {
  Object.defineProperty(HTMLElement.prototype, "scrollHeight", { configurable: true, get: () => 2000 });
  Object.defineProperty(HTMLElement.prototype, "clientHeight", { configurable: true, get: () => 400 });
});

beforeEach(() => {
  vi.stubGlobal("crypto", { ...globalThis.crypto, randomUUID: () => "uuid-1" });
  mocked("conversations").mockResolvedValue([conversation]);
  mocked("sendConversationMessage").mockResolvedValue({ queued: true });
});
afterEach(() => vi.unstubAllGlobals());

async function openScrolledUp(initial: Msg[]) {
  mocked("conversation").mockResolvedValue(detailOf(initial));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const user = userEvent.setup();
  const view = render(
    <QueryClientProvider client={client}><ToastProvider>
      <Customers accountId="acc-1" />
    </ToastProvider></QueryClientProvider>,
  );
  await user.click(await screen.findByText("Khách A"));
  const log = await screen.findByRole("log", { name: "Tin nhắn trong hội thoại" });
  log.scrollTop = 0;
  fireEvent.scroll(log);
  const refetchWith = async (messages: Msg[]) => {
    mocked("conversation").mockResolvedValue(detailOf(messages));
    await act(async () => {
      await client.invalidateQueries({ queryKey: ["conversation", "acc-1", "c1"] });
    });
  };
  return { user, client, log, refetchWith, ...view };
}

describe("Inbox - '↓ Tin nhắn mới' while reading older history", () => {
  it("appears when a new message lands in a short thread", async () => {
    const { refetchWith } = await openScrolledUp(inbound(0, 10));
    await refetchWith(inbound(0, 11));
    await screen.findByText("tin 10");
    expect(screen.getByRole("button", { name: INDICATOR })).toBeInTheDocument();
  });

  it("still appears once the thread fills the 200-message window and a new message shifts it", async () => {
    // The reader returns the newest 200, so m0 drops out as m200 comes in and
    // the count stays exactly 200. Detection must not depend on the count.
    const { refetchWith } = await openScrolledUp(inbound(0, 200));
    await refetchWith(inbound(1, 201));
    await screen.findByText("tin 200");
    expect(screen.getByRole("button", { name: INDICATOR })).toBeInTheDocument();
  });

  it("does not appear when the operator dismisses a failed send - nothing new arrived", async () => {
    mocked("sendConversationMessage").mockRejectedValue(new Error("conversation is not in HUMAN mode"));
    const { user, log } = await openScrolledUp(inbound(0, 5));
    await user.type(screen.getByPlaceholderText("Nhập tin nhắn trả lời khách…"), "lỗi");
    await user.click(screen.getByRole("button", { name: /Gửi$/ }));
    await screen.findByText("Gửi thất bại - tin nhắn chưa đến khách");
    // The operator's own send is a new bubble and may have raised the
    // indicator. Scroll to the bottom (clears it), then back up, so the
    // dismiss below starts from "scrolled up, indicator off".
    log.scrollTop = 1600;
    fireEvent.scroll(log);
    log.scrollTop = 0;
    fireEvent.scroll(log);
    expect(screen.queryByRole("button", { name: INDICATOR })).not.toBeInTheDocument();

    await user.click(within(log).getByRole("button", { name: "Xoá" }));

    expect(screen.queryByText("lỗi")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: INDICATOR })).not.toBeInTheDocument();
  });

  it("does not re-fire when a pending bubble is replaced by its database row", async () => {
    const { user, log, refetchWith } = await openScrolledUp(inbound(0, 5));
    await user.type(screen.getByPlaceholderText("Nhập tin nhắn trả lời khách…"), "gửi đây");
    await user.click(screen.getByRole("button", { name: /Gửi$/ }));
    await screen.findByText("gửi đây");
    log.scrollTop = 1600;
    fireEvent.scroll(log);
    log.scrollTop = 0;
    fireEvent.scroll(log);
    expect(screen.queryByRole("button", { name: INDICATOR })).not.toBeInTheDocument();

    await refetchWith([...inbound(0, 5), {
      message_id: "uuid-1", direction: "OUTBOUND", text: "gửi đây",
      created_at: "2026-09-01T10:01:00Z", source: "human", media: [],
    }]);

    await vi.waitFor(() => expect(screen.getAllByText("gửi đây")).toHaveLength(1));
    expect(screen.queryByRole("button", { name: INDICATOR })).not.toBeInTheDocument();
  });
});

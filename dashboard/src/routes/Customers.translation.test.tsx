import { render, screen } from "@testing-library/react";
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
    api: {
      ...actual.api, conversations: vi.fn(), conversation: vi.fn(), conversationTranslations: vi.fn(),
    },
  };
});

const AUTO: Conversation["control"] = {
  mode: "AUTO", operator_id: null, takeover_source: null, reason: null,
};
const conversation: Conversation = {
  conversation_id: "c1", last_message_at: "2026-10-15T10:00:00Z", message_count: 2, inbound: 1,
  outbound: 1, username: null, display_name: "Khách Myanmar", avatar_url: null, customer_id: "c1",
  intent: null, stage: "new", requires_human: false, summary: null, control: AUTO,
};
const detail: ConversationDetail = {
  conversation_id: "c1", profile: { username: "user" }, facts: [], state: {}, control: AUTO,
  messages: [
    { message_id: "m1", direction: "INBOUND", text: "ဈေးဘယ်လောက်လဲ", created_at: "2026-10-15T10:00:00Z", source: "dom", media: [] },
    { message_id: "m1", direction: "OUTBOUND", text: "၂၅၀၀၀ ကျပ်ပါ", created_at: "2026-10-15T10:01:00Z", source: "dom", media: [] },
  ],
};
const mocked = (name: keyof typeof api) => api[name] as unknown as ReturnType<typeof vi.fn>;

async function open() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}><ToastProvider>
      <Customers accountId="acc-1" />
    </ToastProvider></QueryClientProvider>,
  );
  await userEvent.setup().click(await screen.findByText("Khách Myanmar"));
  await screen.findByText("ဈေးဘယ်လောက်လဲ");
}

beforeEach(() => {
  mocked("conversations").mockResolvedValue([conversation]);
  mocked("conversation").mockResolvedValue(detail);
});

describe("Inbox - Vietnamese translation under a Myanmar shop's messages", () => {
  it("shows each translation under its own bubble, matched by direction too", async () => {
    mocked("conversationTranslations").mockResolvedValue({
      enabled: true, remaining: 0, error: null,
      translations: [
        { message_id: "m1", direction: "INBOUND", text: "Giá bao nhiêu?" },
        { message_id: "m1", direction: "OUTBOUND", text: "25.000 kyat ạ" },
      ],
    });
    await open();
    const question = await screen.findByText("Giá bao nhiêu?");
    expect(question.closest(".message-row")).toHaveClass("inbound");
    expect((await screen.findByText("25.000 kyat ạ")).closest(".message-row")).toHaveClass("outbound");
  });

  it("says why there is no translation instead of failing silently", async () => {
    mocked("conversationTranslations").mockResolvedValue({
      enabled: true, remaining: 1, translations: [],
      error: "Workspace chưa có Gemini API key nên chưa dịch được",
    });
    await open();
    expect(await screen.findByText(/chưa có Gemini API key/)).toBeInTheDocument();
  });

  it("shows nothing extra for a Vietnamese shop", async () => {
    mocked("conversationTranslations").mockResolvedValue({
      enabled: false, remaining: 0, error: null, translations: [],
    });
    await open();
    expect(document.querySelector(".message-translation")).toBeNull();
    expect(screen.queryByText(/Bản dịch:/)).toBeNull();
  });
});

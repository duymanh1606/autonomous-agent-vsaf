import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { MessageComposer } from "./Customers";
import { api } from "../lib/api";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, api: { ...actual.api, uploadChatAttachment: vi.fn() } };
});

beforeAll(() => {
  // jsdom has no object URLs.
  URL.createObjectURL = vi.fn(() => "blob:preview");
  URL.revokeObjectURL = vi.fn();
});

function Harness({ onSend }: { onSend: (text: string, ids: string[]) => void }) {
  const [draft, setDraft] = useState("");
  return (
    <MessageComposer
      accountId="acc-1" enabled disabledReason="" sending={false}
      draft={draft} onDraftChange={setDraft} onSend={onSend}
    />
  );
}

const png = (name = "mau.png", bytes = 10) => new File([new Uint8Array(bytes)], name, { type: "image/png" });

describe("MessageComposer - đính kèm tệp", () => {
  beforeEach(() => vi.clearAllMocks());

  it("uploads a picked picture and sends it, text optional", async () => {
    (api.uploadChatAttachment as ReturnType<typeof vi.fn>).mockResolvedValue({
      id: "a".repeat(32) + ".png", kind: "image", name: "mau.png", size: 10,
    });
    const onSend = vi.fn();
    const user = userEvent.setup();
    render(<Harness onSend={onSend} />);

    await user.upload(screen.getByLabelText("Chọn tệp đính kèm"), png());
    expect(api.uploadChatAttachment).toHaveBeenCalledWith("acc-1", expect.any(File));
    await screen.findByText(/0\.0 MB/);
    expect(screen.getByRole("button", { name: "Bỏ mau.png" })).toBeTruthy();

    await user.click(screen.getByRole("button", { name: /Gửi/ }));
    expect(onSend).toHaveBeenCalledWith("", ["a".repeat(32) + ".png"]);
    expect(screen.queryByRole("button", { name: "Bỏ mau.png" })).toBeNull();
  });

  it("refuses another file type before uploading anything", async () => {
    const user = userEvent.setup({ applyAccept: false });
    render(<Harness onSend={vi.fn()} />);
    await user.upload(
      screen.getByLabelText("Chọn tệp đính kèm"),
      new File(["x"], "tai-lieu.pdf", { type: "application/pdf" }),
    );
    expect(screen.getByRole("alert").textContent).toContain("Chỉ gửi được ảnh");
    expect(api.uploadChatAttachment).not.toHaveBeenCalled();
  });

  it("will not send while an upload has failed", async () => {
    (api.uploadChatAttachment as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("Ảnh tối đa 10 MB"));
    const user = userEvent.setup();
    render(<Harness onSend={vi.fn()} />);
    await user.type(screen.getByPlaceholderText("Nhập tin nhắn trả lời khách…"), "Mẫu đây ạ");
    await user.upload(screen.getByLabelText("Chọn tệp đính kèm"), png());
    await screen.findByText("Ảnh tối đa 10 MB");
    expect((screen.getByRole("button", { name: /Gửi/ }) as HTMLButtonElement).disabled).toBe(true);
  });
});

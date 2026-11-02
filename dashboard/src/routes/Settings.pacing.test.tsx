import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Settings } from "./Settings";
import { api } from "../lib/api";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      readSettings: vi.fn(),
      settings: vi.fn(),
      followups: vi.fn().mockResolvedValue({ rules: [] }),
    },
  };
});

const INTERVAL = "messaging.auto_check.interval_seconds";
const QUIET = "messaging.auto_check.reply_quiet_period_seconds";
const mocked = (fn: unknown) => fn as ReturnType<typeof vi.fn>;

function setup(running: boolean) {
  mocked(api.readSettings).mockResolvedValue({
    values: { [INTERVAL]: 20, [QUIET]: 10 },
    limits: { [INTERVAL]: { min: 5, max: 600 }, [QUIET]: { min: 0, max: 29 } },
  });
  mocked(api.settings).mockResolvedValue({
    applied: {}, restart_required: { [QUIET]: "đã lưu" }, rejected: {},
  });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <Settings accountId="acc-1" running={running} />
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

const quietInput = () => screen.getByLabelText("Chờ tin nhắn trong bao lâu (giây)") as HTMLInputElement;
const applyButton = () => screen.getByRole("button", { name: "Áp dụng" });

describe("Nhịp quét & trả lời", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows the values the workspace uses now", async () => {
    setup(true);
    await waitFor(() => expect(quietInput().value).toBe("10"));
    expect((screen.getByLabelText("Bao lâu quét inbox một lần (giây)") as HTMLInputElement).value).toBe("20");
    expect(applyButton()).toBeDisabled(); // nothing changed yet
  });

  it("can be saved while the agent is stopped, sending only what changed", async () => {
    const user = setup(false);
    await waitFor(() => expect(quietInput().value).toBe("10"));
    expect(screen.getByText(/Agent chưa chạy: thay đổi sẽ được lưu/)).toBeTruthy();

    await user.clear(quietInput());
    await user.type(quietInput(), "15");
    await user.click(applyButton());

    expect(api.settings).toHaveBeenCalledWith("acc-1", { [QUIET]: 15 });
    expect(await screen.findByText("Đã lưu, có hiệu lực khi workspace chạy:")).toBeTruthy();
  });

  it("only lets digits into the boxes", async () => {
    const user = setup(true);
    await waitFor(() => expect(quietInput().value).toBe("10"));

    await user.clear(quietInput());
    await user.type(quietInput(), "aaaa");
    expect(quietInput().value).toBe("");

    await user.type(quietInput(), "1a2-,5.x");
    expect(quietInput().value).toBe("12.5");
  });

  it("refuses a value outside the server's range before sending it", async () => {
    const user = setup(true);
    await waitFor(() => expect(quietInput().value).toBe("10"));

    await user.clear(quietInput());
    await user.type(quietInput(), "30");

    expect(screen.getByText("Trong khoảng 0 - 29 giây.")).toBeTruthy();
    expect(applyButton()).toBeDisabled();
    expect(api.settings).not.toHaveBeenCalled();
  });
});

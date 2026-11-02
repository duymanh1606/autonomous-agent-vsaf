import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Settings } from "./Settings";
import { api } from "../lib/api";
import type { FollowUpRule } from "../lib/api";

/**
 * "Xoá" on a follow-up rule writes the whole rule list back. It used to build
 * that list from the rules as they were at page load, so the first delete
 * worked and the second quietly restored the rule the first had removed -
 * "xoá lúc được lúc không".
 */
vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      followups: vi.fn(),
      saveFollowups: vi.fn(),
      settings: vi.fn().mockResolvedValue({}),
      readSettings: vi.fn().mockResolvedValue({ values: {}, limits: {} }),
      workingHours: vi.fn().mockResolvedValue({}),
    },
  };
});

const mocked = (fn: unknown) => fn as ReturnType<typeof vi.fn>;
const rule = (id: string, message: string): FollowUpRule =>
  ({ id, delay_minutes: 1, message, enabled: true });

let server: FollowUpRule[];

function setup(initial: FollowUpRule[]) {
  server = initial;
  mocked(api.followups).mockImplementation(async () => ({ rules: server }));
  mocked(api.saveFollowups).mockImplementation(async (_: string, rules: FollowUpRule[]) => {
    server = rules.map((item, i) => ({ ...item, id: item.id ?? `new${i}` }));
    return { rules: server };
  });
  render(
    <QueryClientProvider client={new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    })}>
      <Settings accountId="acc-1" running={false} />
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

const messages = () =>
  screen.queryAllByPlaceholderText(/Ví dụ: Dạ shop nhắn hỏi thêm/).map(
    (el) => (el as HTMLTextAreaElement).value,
  );

describe("Follow-up rules - Xoá", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });

  it("deletes one rule after another without bringing earlier ones back", async () => {
    const user = setup([rule("a", "A"), rule("b", "B"), rule("c", "C")]);

    await user.click(await screen.findByLabelText("Xoá luật 1"));
    await waitFor(() => expect(server.map((r) => r.id)).toEqual(["b", "c"]));
    await user.click(await screen.findByLabelText("Xoá luật 1"));
    await waitFor(() => expect(server.map((r) => r.id)).toEqual(["c"]));

    expect(messages()).toEqual(["C"]);
  });

  it("deletes a rule that was only just saved, and nothing else", async () => {
    const user = setup([rule("a", "A")]);

    await user.click(await screen.findByLabelText("Xoá luật 1"));
    await waitFor(() => expect(server).toEqual([]));
    await user.type(screen.getAllByPlaceholderText(/Ví dụ/)[0], "Mới");
    await user.click(screen.getByRole("button", { name: "Lưu" }));
    await waitFor(() => expect(server.map((r) => r.message)).toEqual(["Mới"]));

    await user.click(screen.getByLabelText("Xoá luật 1"));
    await waitFor(() => expect(server).toEqual([]));
  });

  it("only lets a number into the delay box", async () => {
    const user = setup([rule("a", "A")]);
    const delay = (await screen.findByLabelText("Thời gian chờ luật 1")) as HTMLInputElement;
    await user.clear(delay);
    await user.type(delay, "aaaa1,5");
    expect(delay.value).toBe("1.5");
  });

  it("asks first, and does nothing when the operator says no", async () => {
    vi.mocked(window.confirm).mockReturnValue(false);
    const user = setup([rule("a", "A")]);

    await user.click(await screen.findByLabelText("Xoá luật 1"));

    expect(window.confirm).toHaveBeenCalled();
    expect(api.saveFollowups).not.toHaveBeenCalled();
    expect(messages()).toEqual(["A"]);
  });

  it("keeps unsaved edits on the other cards when one rule is deleted", async () => {
    const user = setup([rule("a", "A"), rule("b", "B")]);

    const second = (await screen.findAllByPlaceholderText(/Ví dụ/))[1];
    await user.type(second, " sửa");
    await user.click(screen.getByLabelText("Xoá luật 1"));

    await waitFor(() => expect(server).toEqual([rule("b", "B")]));
    // Written to the server as it was saved, still on screen as edited.
    expect(messages()).toEqual(["B sửa"]);
    expect(screen.getByRole("button", { name: "Lưu" })).toBeEnabled();
  });

  it("does not allow a second delete while the first is still in flight", async () => {
    const user = setup([rule("a", "A"), rule("b", "B")]);
    let finish: () => void = () => {};
    mocked(api.saveFollowups).mockImplementationOnce(
      (_: string, rules: FollowUpRule[]) =>
        new Promise((resolve) => { finish = () => { server = rules; resolve({ rules }); }; }),
    );

    await user.click(await screen.findByLabelText("Xoá luật 1"));
    expect(screen.getByLabelText("Xoá luật 2")).toBeDisabled();

    finish();
    await waitFor(() => expect(messages()).toEqual(["B"]));
  });

  it("leaves the card in place when the server refuses the delete", async () => {
    const user = setup([rule("a", "A")]);
    mocked(api.saveFollowups).mockRejectedValueOnce(new Error("lỗi mạng"));

    await user.click(await screen.findByLabelText("Xoá luật 1"));

    expect(await screen.findByText("lỗi mạng")).toBeTruthy();
    expect(messages()).toEqual(["A"]);
  });
});

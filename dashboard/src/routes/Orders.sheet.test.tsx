import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Orders } from "./Orders";
import { api } from "../lib/api";
import { ToastProvider } from "../components/Toast";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, api: { ...actual.api, orders: vi.fn(), sheetLink: vi.fn() } };
});

const SHEET = "https://docs.google.com/spreadsheets/d/1AbC/edit";

function renderOrders(url: string) {
  (api.orders as ReturnType<typeof vi.fn>).mockResolvedValue([]);
  (api.sheetLink as ReturnType<typeof vi.fn>).mockResolvedValue({ url });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ToastProvider>
        <Orders accountId="acc-1" />
      </ToastProvider>
    </QueryClientProvider>,
  );
  return screen.getByRole("button", { name: "Xuất Google Sheet" });
}

describe("Orders - Xuất Google Sheet", () => {
  it("opens the configured sheet in a new tab", async () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    const button = renderOrders(SHEET);
    await vi.waitFor(() => expect(button).toBeEnabled());
    await userEvent.setup().click(button);
    expect(open).toHaveBeenCalledWith(SHEET, "_blank", "noopener,noreferrer");
    open.mockRestore();
  });

  it("is disabled and says what to set when no sheet is configured", async () => {
    const button = renderOrders("");
    await vi.waitFor(() => expect(api.sheetLink).toHaveBeenCalled());
    expect(button).toBeDisabled();
    expect(button.title).toContain("GSHEET_URL");
  });
});

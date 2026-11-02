/**
 * When the app decides the operator is logged out.
 *
 * Only two things mean that: no user at all, or the server saying 401. A
 * background refetch of "me" that fails for any other reason must not
 * bounce a working operator to the login screen - those refetches now
 * happen on every create/delete, on window focus and on a timer.
 */
import { act, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const meImpl = { current: async (): Promise<any> => ({
  id: 7, email: "staff@shop.vn", is_admin: false, is_active: true, account_ids: [],
}) };

vi.mock("./lib/api", async (importOriginal) => {
  const real = await importOriginal<typeof import("./lib/api")>();
  return {
    ...real,
    openVncWindow: vi.fn(),
    api: {
      ...real.api,
      me: vi.fn(() => meImpl.current()),
      accounts: vi.fn(async () => []),
      notifications: vi.fn(async () => []),
      escalations: vi.fn(async () => []),
    },
  };
});
vi.stubGlobal("WebSocket", class { onopen = null; close() {} });

import App from "./App";
import { ApiError } from "./lib/api";

function renderApp() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><App /></QueryClientProvider>);
  return client;
}

const loggedInMarker = () => screen.queryByRole("heading", { level: 1, name: "Agent TIKTOK" });
const loginScreen = () => screen.queryAllByText(/Đăng nhập|Trung tâm điều khiển/)[0] ?? null;

describe("auth gate", () => {
  beforeEach(() => vi.spyOn(console, "error").mockImplementation(() => {}));

  it("keeps the operator in when a background refetch fails with a network error", async () => {
    const client = renderApp();
    await screen.findByRole("heading", { level: 1, name: "Agent TIKTOK" });

    meImpl.current = async () => { throw new TypeError("Failed to fetch"); };
    await act(async () => { await client.refetchQueries({ queryKey: ["me"] }); });

    // Assert only once the failed refetch has actually landed in state -
    // checking before that proves nothing, the old gate passes it too.
    await waitFor(() => expect(client.getQueryState(["me"])?.status).toBe("error"));
    expect(client.getQueryData(["me"])).toBeTruthy();   // data retained, as React Query does
    expect(loggedInMarker()).toBeInTheDocument();
  });

  it("sends the operator to login when the server says 401", async () => {
    meImpl.current = async () => ({
      id: 7, email: "staff@shop.vn", is_admin: false, is_active: true, account_ids: [],
    });
    const client = renderApp();
    await screen.findByRole("heading", { level: 1, name: "Agent TIKTOK" });

    meImpl.current = async () => { throw new ApiError(401, "Phiên đăng nhập đã hết.", "not authenticated"); };
    await act(async () => { await client.refetchQueries({ queryKey: ["me"] }); });

    await waitFor(() => expect(loggedInMarker()).not.toBeInTheDocument());
    expect(loginScreen()).toBeInTheDocument();
  });

  it("shows login when there was never a user", async () => {
    meImpl.current = async () => { throw new TypeError("Failed to fetch"); };
    renderApp();
    expect((await screen.findAllByText(/Đăng nhập|Trung tâm điều khiển/)).length).toBeGreaterThan(0);
    expect(loggedInMarker()).not.toBeInTheDocument();
  });
});

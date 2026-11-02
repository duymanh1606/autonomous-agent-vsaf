/**
 * A workspace an admin just created must appear without a reload, and nobody
 * else may be offered the button at all.
 *
 * Creating a workspace is admin-only: it writes a config and the next click
 * spawns a Chrome and an agent process on the server. Staff are granted
 * access to a workspace that already exists (Người dùng → Cấp quyền).
 *
 * The reload half is an old bug worth keeping pinned: which workspaces the
 * list shows is filtered by `me.account_ids`, which lives in the "me" query
 * and not in "accounts", so refreshing only "accounts" produced a workspace
 * the list already contained but the filter still hid.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// `ownerId` is not decoration: the workspace quota counts what this user
// *created* (owner_id === 7, the signed-in user below), never what someone
// else made and shared with them - so the two have to be tellable apart.
const account = (id: string, label: string, ownerId = 7) => ({
  id, label, index: 0, cdp_port: 1, control_port: 1,
  config_path: "", profile_dir: "", profile_dir_name: "", has_api_key: false,
  owner_id: ownerId, owner_email: null, database_path: "",
  control_connected: false, database: { ok: true, detail: "" },
  pinned: false, lifecycle_state: "active", idle_seconds: 0,
  processes: { chrome: { running: false, pid: null }, agent: { running: false, pid: null } },
});

// What the fake backend "knows" - mutated by createAccount exactly the way
// the real one grants the creator access as part of creating.
const backend = {
  accounts: [account("ws-old", "Shop cũ")],
  grantedIds: ["ws-old"],
  isAdmin: true,
  maxWorkspaces: null as number | null,
};

vi.mock("./lib/api", async (importOriginal) => {
  const real = await importOriginal<typeof import("./lib/api")>();
  return {
    ...real,
    openVncWindow: vi.fn(),
    api: {
      ...real.api,
      me: vi.fn(async () => ({
        id: 7, email: backend.isAdmin ? "admin@shop.vn" : "staff@shop.vn",
        is_admin: backend.isAdmin, is_active: true,
        account_ids: [...backend.grantedIds],
        max_workspaces: backend.maxWorkspaces,
      })),
      accounts: vi.fn(async () => [...backend.accounts]),
      notifications: vi.fn(async () => []),
      escalations: vi.fn(async () => []),
      status: vi.fn(async () => ({})),
      createAccount: vi.fn(async (label: string) => {
        const created = account("ws-new", label);
        backend.accounts.push(created);
        backend.grantedIds.push(created.id);
        return created;
      }),
      start: vi.fn(async () => ({})),
      focusWorkspaceWindow: vi.fn(async () => ({})),
    },
  };
});

// The sidebar opens a WebSocket for live events; jsdom has none.
vi.stubGlobal("WebSocket", class {
  onopen: null | (() => void) = null;
  close() {}
});

import App from "./App";

function renderApp() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><App /></QueryClientProvider>);
}

describe("creating a workspace", () => {
  beforeEach(() => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    backend.isAdmin = true;
    backend.maxWorkspaces = null;
    // createAccount below pushes into these, and the quota assertions count
    // them - so each test starts from the same single workspace.
    backend.accounts = [account("ws-old", "Shop cũ")];
    backend.grantedIds = ["ws-old"];
  });

  it("shows the new workspace in the switcher without a reload", async () => {
    renderApp();
    // The existing workspace is up, so the app has finished loading.
    await screen.findByText("Shop cũ", {}, { timeout: 3000 });

    // Open the switcher, choose "add", type a name, submit.
    await userEvent.click(screen.getByRole("button", { name: /Chọn workspace khác/ }));
    await userEvent.click(screen.getByRole("button", { name: /Thêm workspace/ }));
    await userEvent.type(screen.getByRole("textbox"), "Shop mới");
    await userEvent.keyboard("{Enter}");

    // The bug: the accounts list refreshed, but `me.account_ids` did not,
    // so the filter hid "Shop mới" until a full page reload.
    await waitFor(
      () => expect(screen.getAllByText("Shop mới").length).toBeGreaterThan(0),
      { timeout: 3000 },
    );
  });

  it("is not offered to a non-admin an admin has granted no quota", async () => {
    backend.isAdmin = false;
    backend.maxWorkspaces = null;
    backend.accounts = [account("ws-old", "Shop cũ", 99)];
    renderApp();
    await screen.findByText("Shop cũ", {}, { timeout: 3000 });

    await userEvent.click(screen.getByRole("button", { name: /Chọn workspace khác/ }));

    // null is 0, not "unlimited" - the API refuses this user anyway, and a
    // button that always 403s is worse than no button. The workspace they
    // can see ("ws-old") was granted to them, which is a different thing.
    expect(screen.queryByRole("button", { name: /Thêm workspace/ })).toBeNull();
  });

  it("is offered within the quota an admin granted, and spent at the limit", async () => {
    backend.isAdmin = false;
    backend.maxWorkspaces = 1;
    // Owned by someone else and merely granted: it must not eat the quota.
    backend.accounts = [account("ws-old", "Shop cũ", 99)];
    renderApp();
    await screen.findByText("Shop cũ", {}, { timeout: 3000 });
    await userEvent.click(screen.getByRole("button", { name: /Chọn workspace khác/ }));

    // "ws-old" is owned by someone else and merely granted, so none of the
    // quota is spent yet: 0 of 1.
    const add = screen.getByRole("button", { name: /Thêm workspace \(0\/1\)/ });
    expect(add).toBeEnabled();

    await userEvent.click(add);
    await userEvent.type(screen.getByRole("textbox"), "Shop của tôi");
    await userEvent.keyboard("{Enter}");
    await waitFor(
      () => expect(screen.getAllByText("Shop của tôi").length).toBeGreaterThan(0),
      { timeout: 3000 },
    );

    // Now they own one of their one: the button stays visible (so the count
    // explains why) but is refused before it can 403.
    await userEvent.click(screen.getByRole("button", { name: /Chọn workspace khác/ }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Thêm workspace \(1\/1\)/ })).toBeDisabled(),
    );
  });
});

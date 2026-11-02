import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, openVncWindow } from "./lib/api";
import type { AgentEvent, AgentStatus, User } from "./lib/api";
import { notifyForEvent } from "./lib/browserNotifications";
import { onPushOpen, syncPush, takeLaunchTarget } from "./lib/webPush";
import { useEvents } from "./hooks/useEvents";
import { Sidebar } from "./components/Sidebar";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { ToastProvider, useToast } from "./components/Toast";
import { Topbar } from "./components/Topbar";
import { ForceChangePassword } from "./components/ForceChangePassword";
import { CommandPalette } from "./components/CommandPalette";
import { Dashboard } from "./routes/Dashboard";
import { Customers } from "./routes/Customers";
import { Orders } from "./routes/Orders";
import { Products } from "./routes/Products";
import { Settings as Automation } from "./routes/Settings";
import { UsersAdmin } from "./routes/UsersAdmin";
import { LogsPage } from "./routes/LogsPage";
import { Scenario } from "./routes/Scenario";
import { ActivityPage } from "./routes/ActivityPage";
import { AlertsPage } from "./routes/AlertsPage";
import { Auth } from "./routes/Auth";
import { ResetPassword } from "./routes/ResetPassword";

export type Page = "home" | "inbox" | "orders" | "products" | "automation" | "scenario" | "activity" | "alerts" | "users" | "logs";

const PAGE_LABEL: Record<Page, string> = {
  home: "Tổng quan",
  inbox: "Tin nhắn",
  orders: "Đơn hàng",
  products: "Sản phẩm",
  automation: "Tự động hóa",
  scenario: "Kịch bản",
  activity: "Hoạt động",
  alerts: "Cảnh báo",
  users: "Người dùng",
  logs: "Nhật ký hệ thống",
};

function AuthedApp({ me, onLogout }: { me: User; onLogout: () => void }) {
  const toast = useToast();
  const client = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const [page, setPage] = useState<Page>("home");
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [focusConversationId, setFocusConversationId] = useState<string | null>(null);
  // Deep links out of the ⌘K palette into one order / one product.
  const [focusOrderId, setFocusOrderId] = useState<number | null>(null);
  const [focusProductId, setFocusProductId] = useState<string | null>(null);
  // Set below, once the visible workspaces are known. A ref so the event
  // stream (subscribed once) always calls the current version.
  const notifyRef = useRef<(event: AgentEvent) => void>(() => {});
  const { connection, events } = useEvents((event) => notifyRef.current(event));

  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: api.accounts,
    refetchInterval: 5000,
  });

  // The backend returns every workspace to an admin (needed so the "Người
  // dùng" access-grant page can offer any workspace, see `allAccounts`
  // below) - everywhere else in the app an admin sees only their own
  // workspaces, same as a regular user, not everyone else's. `owner_id ==
  // null` covers workspaces created before ownership existed; those stay
  // visible to admins (who were the only ones who could create them then)
  // rather than becoming orphaned and invisible to everyone.
  const allAccounts = accounts.data ?? [];
  const visibleAccounts = useMemo(
    () =>
      me.is_admin
        ? allAccounts.filter((item) => item.owner_id === me.id || item.owner_id == null)
        : allAccounts.filter((item) => me.account_ids.includes(item.id)),
    [allAccounts, me],
  );

  // Desktop notifications (lib/browserNotifications.ts). Only for workspaces
  // this person works in: an admin's WebSocket carries every workspace's
  // events, and other owners' customers are not theirs to be woken up for.
  // Where a clicked notification lands: the conversation for an escalation,
  // the alerts page for anything else. Only state setters, so stable.
  const openFromNotification = (accountId: string, conversationId: string | null) => {
    setSelected(accountId);
    if (conversationId) {
      setFocusConversationId(conversationId);
      setPage("inbox");
    } else {
      setPage("alerts");
    }
  };
  notifyRef.current = (event) => {
    const account = visibleAccounts.find((item) => item.id === event.account_id);
    if (!account) return;
    notifyForEvent(event, account.label || account.id, () => {
      const conversationId = event.payload?.conversation_id;
      openFromNotification(
        account.id,
        event.type === "escalation.opened" && typeof conversationId === "string" ? conversationId : null,
      );
    });
  };

  // Web Push (lib/webPush.ts): keep this browser's subscription current, and
  // follow a clicked push - opened in a new tab (query string) or sent to
  // this one by the service worker.
  useEffect(() => {
    void syncPush();
    const launch = takeLaunchTarget();
    if (launch?.account_id) openFromNotification(launch.account_id, launch.conversation_id);
    return onPushOpen((target) => {
      if (target.account_id) openFromNotification(target.account_id, target.conversation_id);
    });
  }, []);

  const notifications = useQuery({
    queryKey: ["notifications"],
    queryFn: api.notifications,
    refetchInterval: 10000,
  });

  // Every account's status, not just the open one, so a workspace that hits a
  // CAPTCHA is visible in the sidebar/alerts without clicking into it.
  const statusQueries = useQueries({
    queries: visibleAccounts.map((account) => ({
      queryKey: ["status", account.id],
      queryFn: () => api.status(account.id),
      enabled: account.processes.agent.running && account.control_connected,
      refetchInterval: 5000,
      retry: false,
    })),
  });

  const statuses = useMemo(() => {
    const map: Record<string, AgentStatus | undefined> = {};
    visibleAccounts.forEach((account, index) => {
      map[account.id] = statusQueries[index]?.data;
    });
    return map;
  }, [visibleAccounts, statusQueries]);

  // Only auto-picks a workspace once, on first load with no selection -
  // guarded by a ref (not just `!selected`) so a deliberate deselect (a
  // workspace card toggled off, see Dashboard.tsx) sticks instead of
  // snapping back to the first workspace on the very next render. This also
  // means: not viewing any workspace's Agent page is now a real, reachable
  // state - relevant for the sleep/wake heartbeat, which only runs while a
  // workspace is actually selected.
  const didAutoSelect = useRef(false);
  useEffect(() => {
    if (!didAutoSelect.current && !selected && visibleAccounts.length) {
      setSelected(visibleAccounts[0].id);
      didAutoSelect.current = true;
    }
  }, [visibleAccounts, selected]);

  // Cmd/Ctrl+K opens the command palette from anywhere in the app.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((value) => !value);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  // Same call the Dashboard card's toggle makes; the sidebar just offers it
  // without the trip to that page.
  const startWorkspace = useMutation({
    mutationFn: (id: string) => api.start(id, "langgraph", true),
    onSettled: () => client.invalidateQueries({ queryKey: ["accounts"] }),
  });
  const stopWorkspace = useMutation({
    mutationFn: (id: string) => api.stop(id),
    onSettled: () => client.invalidateQueries({ queryKey: ["accounts"] }),
  });

  const create = useMutation({
    mutationFn: (label: string) => api.createAccount(label),
    // The backend refuses with a 403 once a non-admin reaches their
    // max_workspaces; without this the click just did nothing visible.
    onError: (err: Error) => toast.error("Không tạo được workspace", err.message),
    onSuccess: async (account: any) => {
      // Which workspaces a non-admin may see comes from `me.account_ids`,
      // and the backend grants the creator access as part of creating one.
      // That grant lives in the "me" query, not "accounts" - so refreshing
      // only "accounts" produced a workspace the list already had but the
      // filter still hid, and the operator had to reload the page to see
      // what they had just made. Both must refresh, and "me" has to land
      // before the selection below can resolve to a visible workspace.
      await Promise.all([
        client.invalidateQueries({ queryKey: ["me"] }),
        client.invalidateQueries({ queryKey: ["accounts"] }),
      ]);
      setSelected(account.id);
      setPage("home");
      // Open Chrome right away so the operator can log into TikTok Shop for
      // the first time - the session then lives in that Chrome profile for
      // every later Start, and the DOM-bridge extension loads automatically
      // (dashboard_api/processes.py's start_chrome). Best-effort: a failure
      // here just leaves the workspace off, same as any other Start failure -
      // it does not undo the account that was just created.
      try {
        await api.start(account.id, "langgraph", true);
        // Chrome runs on this workspace's own dedicated display, not visible
        // in this browser tab on its own - pop its noVNC tab so the operator
        // lands straight on the TikTok login screen instead of having to
        // separately click "Xem Chrome (VNC)" afterward.
        try {
          await api.focusWorkspaceWindow(account.id);
        } catch {
          // Chrome may still be a moment from coming up; the operator can
          // still open VNC by hand from the workspace page.
        }
        openVncWindow(account);
      } catch {
        // surfaced next via the accounts list showing it still off
      } finally {
        client.invalidateQueries({ queryKey: ["accounts"] });
      }
    },
  });

  const update = useMutation({
    mutationFn: ({ id, fields }: { id: string; fields: { label?: string; api_key?: string } }) =>
      api.updateAccount(id, fields),
    onSuccess: () => client.invalidateQueries({ queryKey: ["accounts"] }),
  });

  const deleteWorkspace = useMutation({
    mutationFn: ({ id, purge }: { id: string; purge: boolean }) => api.deleteAccount(id, purge),
    onSuccess: (_result, { id }) => {
      client.invalidateQueries({ queryKey: ["accounts"] });
      client.invalidateQueries({ queryKey: ["me"] });
      setSelected((current) => (current === id ? null : current));
    },
  });

  const account = visibleAccounts.find((item) => item.id === selected);

  // Sleep/wake (dashboard_api/sleep_wake.py): an operator on any page of a
  // workspace - the Inbox above all, where they wait for customers' messages -
  // keeps it awake, and wakes it if it was asleep. This used to run on the
  // workspace's home page only, so a workspace fell asleep under an operator
  // reading its Inbox, and new messages surfaced only at the next periodic
  // wake (every ~4 minutes).
  const heartbeatAccountId = account?.id;
  useEffect(() => {
    if (!heartbeatAccountId) return;
    const beat = () => {
      api.heartbeat(heartbeatAccountId).catch(() => {});
    };
    beat();
    const interval = window.setInterval(beat, 15000);
    return () => window.clearInterval(interval);
  }, [heartbeatAccountId]);

  // Escalations across every known account, not just the selected one -
  // Alerts (and the sidebar badge) must surface a problem in any workspace,
  // matching how Notifications already worked account-agnostically.
  const escalationQueries = useQueries({
    queries: visibleAccounts.map((acc) => ({
      queryKey: ["escalations", acc.id],
      queryFn: () => api.escalations(acc.id),
      refetchInterval: 15000,
    })),
  });
  const escalationsByAccount = useMemo(() => {
    const map: Record<string, import("./lib/api").Escalation[] | undefined> = {};
    visibleAccounts.forEach((acc, index) => {
      map[acc.id] = escalationQueries[index]?.data;
    });
    return map;
  }, [visibleAccounts, escalationQueries]);

  const alertCount =
    (notifications.data?.length ?? 0) +
    Object.values(escalationsByAccount).reduce((sum, list) => sum + (list?.length ?? 0), 0);

  return (
    <div className="app">
      <Sidebar
        accounts={visibleAccounts}
        statuses={statuses}
        selected={selected}
        connection={connection}
        onSelect={(id) => setSelected(id)}
        onCreate={(label) => create.mutate(label)}
        onDelete={async (id, purge) => {
          await deleteWorkspace.mutateAsync({ id, purge });
        }}
        onStart={(id) => startWorkspace.mutate(id)}
        onStop={(id) => stopWorkspace.mutate(id)}
        createLimit={
          me.is_admin
            ? null
            : {
                // null from the backend means "no quota granted", which is 0
                // - not unlimited. Only admins are uncapped.
                max: me.max_workspaces ?? 0,
                owned: allAccounts.filter((item) => item.owner_id === me.id).length,
              }
        }
        busyId={
          startWorkspace.isPending ? startWorkspace.variables ?? null
          : stopWorkspace.isPending ? stopWorkspace.variables ?? null
          : null
        }
        page={page}
        onNavigate={setPage}
        alertCount={alertCount}
        isAdmin={me.is_admin}
      />

      <div className="canvas">
        <Topbar
          connection={connection}
          notificationCount={alertCount}
          onOpenAlerts={() => setPage("alerts")}
          onOpenSearch={() => setPaletteOpen(true)}
          user={me}
          onLogout={onLogout}
        />

        <div className="workspace-tab-content">
          {/* Sits right above each page's own title - the small uppercase
              one in Topbar reads as chrome and is easy to stop noticing;
              this one is where the eye already lands. Skipped on Dashboard
              itself ("Trang chủ / Dashboard" would just repeat "you are
              home"). Several pages (Dashboard, Người dùng) share the same
              4-stat-card header shape, which read as the same page until
              this landed - see PAGE_LABEL below for the current page name. */}
          {page !== "home" && (
            <nav className="page-breadcrumb" aria-label="Breadcrumb">
              <button type="button" onClick={() => setPage("home")}>Trang chủ</button>
              <span aria-hidden="true">/</span>
              <strong>{PAGE_LABEL[page]}</strong>
            </nav>
          )}
          {/* One boundary per page: a render error in Products must not take
              the sidebar and every other page down with it. Keyed on page and
              workspace so navigating away clears the crash. */}
          <ErrorBoundary label={PAGE_LABEL[page]} resetKey={`${page}:${selected ?? ""}`}>
          {page === "home" ? (
            <Dashboard
              accounts={visibleAccounts}
              statuses={statuses}
              selected={selected}
              onSelect={(id) => setSelected(id)}
              onCreate={(label) => create.mutate(label)}
              onUpdate={async (id, fields) => {
                await update.mutateAsync({ id, fields });
              }}
              onDelete={async (id, purge) => {
                await deleteWorkspace.mutateAsync({ id, purge });
              }}
              escalationsByAccount={escalationsByAccount}
              alertCount={alertCount}
              events={events}
              onOpenInbox={() => setPage("inbox")}
              isAdmin={me.is_admin}
              mayCreate={
                me.is_admin || (me.max_workspaces ?? 0) > 0
              }
            />
          ) : page === "users" ? (
            me.is_admin ? (
              <UsersAdmin accounts={allAccounts} currentUserId={me.id} />
            ) : (
              <main className="main">
                <h2>Không có quyền truy cập</h2>
                <p className="sub">Chỉ admin mới quản lý được người dùng.</p>
              </main>
            )
          ) : page === "logs" ? (
            me.is_admin ? (
              <LogsPage accounts={allAccounts} initialAccountId={selected} />
            ) : (
              <main className="main">
                <h2>Không có quyền truy cập</h2>
                <p className="sub">Chỉ admin mới xem được log.</p>
              </main>
            )
          ) : !account ? (
            <main className="main">
              <h2>Chưa chọn workspace</h2>
              <p className="sub">
                Mỗi workspace là một Chrome profile và một tài khoản TikTok riêng, có
                database, cổng CDP và cổng bridge riêng. Tạo một workspace ở Dashboard
                để bắt đầu.
              </p>
            </main>
          ) : (
            <>
              {page === "inbox" && (
                <Customers
                  accountId={account.id}
                  focusConversationId={focusConversationId}
                  onFocused={() => setFocusConversationId(null)}
                  events={events}
                />
              )}
              {page === "orders" && (
                <Orders
                  accountId={account.id}
                  focusId={focusOrderId}
                  onFocused={() => setFocusOrderId(null)}
                />
              )}
              {page === "products" && (
                <Products
                  accountId={account.id}
                  focusId={focusProductId}
                  onFocused={() => setFocusProductId(null)}
                />
              )}
              {page === "automation" && (
                <div className="main">
                  <header>
                    <h2>Cấu hình Tự động hóa</h2>
                  </header>
                  <p className="sub">
                    Nhịp quét inbox, chờ tin nhắn trước khi trả lời, và tự động nhắc
                    khách im lặng. Agent đang chạy thì áp dụng ngay; chưa chạy thì lưu lại,
                    có hiệu lực khi workspace chạy.
                  </p>
                  <Automation
                    accountId={account.id}
                    running={
                      account.processes.agent.running && account.control_connected
                    }
                  />
                </div>
              )}
              {page === "scenario" && <Scenario accountId={account.id} />}
              {page === "activity" && (
                <ActivityPage account={account} events={events} />
              )}
              {page === "alerts" && (
                <AlertsPage
                  accounts={visibleAccounts}
                  statuses={statuses}
                  notifications={notifications.data ?? []}
                  escalationsByAccount={escalationsByAccount}
                  onOpenInbox={(accountId, conversationId) => {
                    setSelected(accountId);
                    setFocusConversationId(conversationId);
                    setPage("inbox");
                  }}
                  onOpenWorkspace={(accountId) => {
                    setSelected(accountId);
                    setPage("home");
                  }}
                />
              )}
            </>
          )}
          </ErrorBoundary>
        </div>
      </div>

      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        accountId={selected}
        accounts={visibleAccounts}
        onNavigate={setPage}
        onSelectWorkspace={(id) => {
          setSelected(id);
          setPage("home");
        }}
        onOpenConversation={(conversationId) => {
          setFocusConversationId(conversationId);
          setPage("inbox");
        }}
        onOpenOrder={(orderId) => {
          setFocusOrderId(orderId);
          setPage("orders");
        }}
        onOpenProduct={(productId) => {
          setFocusProductId(productId);
          setPage("products");
        }}
      />
    </div>
  );
}

// The app has no URL router - navigation between workspace pages is plain
// `useState`. A reset-password link still needs a real, clickable URL to
// land on, so that one path is read straight off `window.location` here,
// bypassing the whole state-based scheme.
function resetPasswordToken(): string | null | undefined {
  if (window.location.pathname !== "/reset-password") return undefined;
  return new URLSearchParams(window.location.search).get("token");
}

export default function App() {
  const client = useQueryClient();
  const resetToken = resetPasswordToken();
  const me = useQuery({
    queryKey: ["me"],
    queryFn: api.me,
    retry: false,
    enabled: resetToken === undefined,
    // `account_ids` on this row is what decides which workspaces a non-admin
    // sees. An admin granting them a workspace changes it on the server
    // only; without this, that grant showed up on the next page reload and
    // not before. Safe to poll because the auth gate below no longer treats
    // a failed refetch as "logged out".
    refetchInterval: 30_000,
  });

  if (resetToken !== undefined) {
    return <ResetPassword token={resetToken} />;
  }

  if (me.isLoading) {
    return (
      <div className="auth-screen">
        <div className="auth-card">
          <h1>Trung tâm điều khiển</h1>
          <p className="sub">Đang tải...</p>
        </div>
      </div>
    );
  }

  // "Logged out" means one of two things: there was never a user (the first
  // load failed, whatever the reason), or the server has now said 401. A
  // *background* refetch failing for any other reason - a dropped
  // connection, the supervisor restarting - keeps the user it already has:
  // React Query retains `data` through a failed refetch, and bouncing a
  // working operator to the login screen for a network blip loses their
  // place for nothing. The refetches on create/delete and on window focus
  // all go through here, so this branch is hit more than it looks.
  const sessionGone = me.error instanceof ApiError && me.error.status === 401;
  if (!me.data || (me.isError && sessionGone)) {
    return <Auth onSuccess={(user) => client.setQueryData(["me"], user)} />;
  }

  const logout = async () => {
        // Show the login screen immediately - don't wait on the network
        // round trip, and don't let a failed/timed-out logout call (session
        // already expired, connection drop) strand the user on the old page.
        // Everything except "me" is dropped so a later login doesn't briefly
        // show this account's stale workspaces; "me" itself is deliberately
        // left alone (not cleared/refetched) - clearing it would flip the
        // query back to "loading" and re-fetch /api/auth/me, which briefly
        // re-shows the loading screen instead of the login form and can get
        // stuck there if that refetch stalls.
        client.setQueryData(["me"], null);
        client.removeQueries({ predicate: (query) => query.queryKey[0] !== "me" });
        try {
          await api.logout();
        } catch {
          // The user is already treated as logged out locally regardless of
          // whether this network call actually reached the server.
        }
  };

  // Logged in with a mailed temporary password: nothing but choosing a new
  // one until that is done (the server enforces the same).
  if (me.data.must_change_password) {
    return (
      <ForceChangePassword
        user={me.data}
        onChanged={() => client.setQueryData(["me"], { ...me.data, must_change_password: false })}
        onLogout={logout}
      />
    );
  }

  return (
    <ToastProvider>
      <AuthedApp me={me.data} onLogout={logout} />
    </ToastProvider>
  );
}

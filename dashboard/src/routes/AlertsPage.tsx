import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { Account, AgentStatus, Escalation, Notification } from "../lib/api";
import { api, openVncWindow } from "../lib/api";
import { AlertTriangleIcon, BellIcon, XCircleIcon } from "../components/ui/icons";

type Severity = "red" | "amber" | "blue";

type Alert = {
  key: string;
  severity: Severity;
  title: string;
  accountLabel: string;
  accountId: string;
  detail?: string;
  conversationId?: string;
  resolvable?: number; // notification id, if this alert can be dismissed
};

function severityFromNotification(notification: Notification): Severity {
  if (notification.severity === "bad") return "red";
  if (notification.severity === "warn") return "amber";
  return "blue";
}

// CAPTCHA/challenge/auth are never stored as notification rows - they live
// only in the live AgentStatus poll (see api.ts's workspaceState()), so
// Alerts derives them the same way the sidebar/topbar status dot already
// does, instead of duplicating that logic against a second data source.
function statusAlerts(account: Account, status: AgentStatus | undefined): Alert[] {
  const alerts: Alert[] = [];
  if (!status) return alerts;
  if (status.security_challenge) {
    alerts.push({
      key: `${account.id}-captcha`,
      severity: "red",
      title: "CAPTCHA/xác minh cần xử lý",
      accountLabel: account.label,
      accountId: account.id,
      detail: "Agent đã tạm dừng thao tác cho đến khi được xử lý thủ công.",
    });
  }
  const humanSession = status.sessions.find((session) => session.human_reason);
  if (humanSession) {
    alerts.push({
      key: `${account.id}-session-${humanSession.session_id}`,
      severity: "red",
      title: "Cần thao tác thủ công trên trình duyệt",
      accountLabel: account.label,
      accountId: account.id,
      detail: humanSession.human_reason ?? undefined,
      conversationId: humanSession.conversation ?? undefined,
    });
  }
  if (status.auth_state && status.auth_state !== "LOGGED_IN") {
    alerts.push({
      key: `${account.id}-auth`,
      severity: "red",
      title: "Cần đăng nhập lại TikTok",
      accountLabel: account.label,
      accountId: account.id,
      detail: status.auth_state,
    });
  }
  return alerts;
}

function escalationAlert(account: Account, escalation: Escalation): Alert {
  return {
    key: `escalation-${escalation.id}`,
    severity: "amber",
    title: "Hội thoại cần người xử lý",
    accountLabel: account.label,
    accountId: account.id,
    detail: escalation.reason,
    conversationId: escalation.conversation_id,
  };
}

function notificationAlert(account: Account | undefined, notification: Notification): Alert {
  const conversationId =
    (notification.payload?.["conversation_id"] as string | undefined) ??
    (notification.payload?.["conversation"] as string | undefined);
  return {
    key: `notification-${notification.id}`,
    severity: severityFromNotification(notification),
    title: notification.title,
    accountLabel: account?.label ?? notification.account_id ?? "Hệ thống",
    accountId: notification.account_id,
    detail: notification.detail ?? undefined,
    conversationId,
    resolvable: notification.id,
  };
}

const SEVERITY_ORDER: Record<Severity, number> = { red: 0, amber: 1, blue: 2 };

export function AlertsPage({
  accounts, statuses, notifications, escalationsByAccount, onOpenInbox, onOpenWorkspace,
}: {
  accounts: Account[];
  statuses: Record<string, AgentStatus | undefined>;
  notifications: Notification[];
  escalationsByAccount: Record<string, Escalation[] | undefined>;
  onOpenInbox: (accountId: string, conversationId: string) => void;
  onOpenWorkspace: (accountId: string) => void;
}) {
  const client = useQueryClient();
  const resolve = useMutation({
    mutationFn: api.resolveNotification,
    onSuccess: () => client.invalidateQueries({ queryKey: ["notifications"] }),
  });
  const [focusError, setFocusError] = useState<string | null>(null);
  const accountById = new Map(accounts.map((account) => [account.id, account]));
  const focusWindow = useMutation({
    mutationFn: api.focusWorkspaceWindow,
    // mutationFn is passed directly (not wrapped), so the only way to know
    // which account this call was for is the mutation's own second
    // onSuccess argument (its variables - the accountId passed to .mutate
    // below), not a closure - there is no per-account instance of this
    // mutation the way HomePage's is scoped to one workspace's page.
    onSuccess: (_data, accountId) => {
      setFocusError(null);
      const account = accountById.get(accountId);
      if (account) openVncWindow(account);
    },
    onError: (err: Error) => setFocusError(err.message),
  });

  const alerts: Alert[] = [
    ...accounts.flatMap((account) => statusAlerts(account, statuses[account.id])),
    ...accounts.flatMap((account) =>
      (escalationsByAccount[account.id] ?? []).map((escalation) => escalationAlert(account, escalation)),
    ),
    ...notifications.map((notification) => notificationAlert(accountById.get(notification.account_id), notification)),
  ].sort((a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity]);

  const redCount = alerts.filter((alert) => alert.severity === "red").length;
  const amberCount = alerts.filter((alert) => alert.severity === "amber").length;
  const blueCount = alerts.filter((alert) => alert.severity === "blue").length;

  return (
    <main className="main">
      <header>
        <h2>Cảnh báo</h2>
      </header>
      <p className="sub">
        CAPTCHA, đăng nhập, mất kết nối trình duyệt và hội thoại cần người xử lý - gộp
        từ mọi workspace.
      </p>

      {focusError && (
        <p className="sub" style={{ color: "var(--red)" }}>
          Không mở được cửa sổ Chrome: {focusError}
        </p>
      )}

      {alerts.length > 0 && (
        <div className="stat-cards" style={{ marginBottom: 20 }}>
          <div className="stat-card tint-danger">
            <XCircleIcon className="stat-card-decor" width={64} height={64} />
            <div className="stat-card-top">
              <span className="stat-card-eyebrow">Khẩn cấp</span>
              <span className="stat-card-icon danger"><XCircleIcon width={18} height={18} /></span>
            </div>
            <div className="n">{redCount}</div>
            <div className="t">Cần xử lý ngay</div>
          </div>
          <div className="stat-card tint-warn">
            <AlertTriangleIcon className="stat-card-decor" width={64} height={64} />
            <div className="stat-card-top">
              <span className="stat-card-eyebrow">Cần chú ý</span>
              <span className="stat-card-icon warn"><AlertTriangleIcon width={18} height={18} /></span>
            </div>
            <div className="n">{amberCount}</div>
            <div className="t">Hội thoại chờ tiếp quản</div>
          </div>
          <div className="stat-card tint-info">
            <BellIcon className="stat-card-decor" width={64} height={64} />
            <div className="stat-card-top">
              <span className="stat-card-eyebrow">Thông báo</span>
              <span className="stat-card-icon info"><BellIcon width={18} height={18} /></span>
            </div>
            <div className="n">{blueCount}</div>
            <div className="t">Không khẩn cấp</div>
          </div>
        </div>
      )}

      {alerts.length === 0 ? (
        <p className="empty">Mọi thứ đang hoạt động bình thường.</p>
      ) : (
        <div className="alert-list">
          {alerts.map((alert) => (
            <div className={`alert-card severity-${alert.severity}`} key={alert.key}>
              <div className="alert-card-main">
                <div className="alert-card-title">
                  <span className={`severity-dot severity-dot-${alert.severity}`} />
                  {alert.title}
                </div>
                <div className="alert-card-meta">{alert.accountLabel}</div>
                {alert.detail && <div className="alert-card-detail">{alert.detail}</div>}
              </div>
              <div className="alert-card-actions">
                <button
                  disabled={focusWindow.isPending}
                  onClick={() => {
                    setFocusError(null);
                    focusWindow.mutate(alert.accountId);
                  }}
                >
                  Xem Chrome (VNC)
                </button>
                {alert.conversationId && (
                  <button onClick={() => onOpenInbox(alert.accountId, alert.conversationId!)}>
                    Mở Tin nhắn
                  </button>
                )}
                {!alert.conversationId && (
                  <button onClick={() => onOpenWorkspace(alert.accountId)}>Mở workspace</button>
                )}
                {alert.resolvable != null && (
                  <button
                    disabled={resolve.isPending}
                    onClick={() => resolve.mutate(alert.resolvable!)}
                  >
                    Đã xử lý
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </main>
  );
}

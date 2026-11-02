import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Account, AgentEvent, AgentStatus } from "../lib/api";
import { WORKSPACE_STATE_LABEL, api, openVncWindow, workspaceState } from "../lib/api";
import { AlertTriangleIcon, CheckCircleIcon, MessageIcon, UsersIcon } from "../components/ui/icons";
import { capabilityLabel, eventLabel, runtimeLabel } from "../lib/labels";
import { useToast } from "../components/Toast";

// Ẩn khu vực "chi tiết kỹ thuật" khỏi mọi người dùng, kể cả admin - đổi
// thành true để hiện lại cho admin.
const SHOW_TECHNICAL_INFO = false;

const AUTH_LABEL: Record<string, string> = {
  LOGGED_IN: "Đã đăng nhập TikTok",
  LOGGED_OUT: "Chưa đăng nhập TikTok",
  UNCONFIRMED: "Đang xác thực phiên đăng nhập",
};

function uptime(seconds?: number): string {
  if (!seconds || seconds < 0) return "—";
  if (seconds < 3600) return `${Math.round(seconds / 60)} phút`;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.round((seconds % 3600) / 60);
  return `${hours} giờ ${minutes} phút`;
}

export function HomePage({
  account, accounts, statuses, events, onOpenInbox, isAdmin,
}: {
  account: Account;
  accounts: Account[];
  statuses: Record<string, AgentStatus | undefined>;
  events: AgentEvent[];
  onOpenInbox: () => void;
  isAdmin: boolean;
}) {
  const client = useQueryClient();
  const toast = useToast();
  // One runtime. The older Extension mode was removed from the backend, and
  // its picker here had been hidden behind SHOW_TECHNICAL_INFO long before
  // that; `api.start` still takes the name, so it is passed explicitly.
  const runtime = "langgraph";
  const [advancedOpen, setAdvancedOpen] = useState(false);

  const running = account.processes.agent.running;
  const connected = account.control_connected;
  const status = statuses[account.id];
  const state = workspaceState(account, status);

  const summary = useQuery({
    queryKey: ["summary", account.id],
    queryFn: () => api.summary(account.id),
    refetchInterval: 10000,
  });
  const health = useQuery({
    queryKey: ["health", account.id],
    queryFn: () => api.health(account.id),
    enabled: running && connected,
    refetchInterval: 15000,
    retry: false,
  });
  const currentTask = useQuery({
    queryKey: ["tasks", account.id, "current", status?.current_task_id],
    queryFn: () => api.tasks(account.id, 20),
    enabled: Boolean(status?.current_task_id),
    refetchInterval: 5000,
  });
  // The sleep/wake heartbeat for this workspace is sent from App.tsx, for
  // every page of the selected workspace rather than this one only.

  const setPinned = useMutation({
    mutationFn: (pinned: boolean) => api.setPinned(account.id, pinned),
    onSuccess: () => client.invalidateQueries({ queryKey: ["accounts"] }),
  });

  const act = useMutation({
    mutationFn: async (action: () => Promise<unknown>) => action(),
    onSuccess: () => client.invalidateQueries({ queryKey: ["accounts"] }),
    onError: (err: Error) => toast.error("Thao tác với workspace thất bại", err.message),
  });

  const focusWindow = useMutation({
    mutationFn: () => api.focusWorkspaceWindow(account.id),
    onSuccess: () => openVncWindow(account),
    onError: (err: Error) => toast.error("Không mở được cửa sổ Chrome", err.message),
  });

  const runningAccounts = accounts.filter(
    (item) => item.processes.agent.running && item.control_connected,
  );
  const problemAccounts = accounts.filter((item) => {
    const s = workspaceState(item, statuses[item.id]);
    return s === "captcha" || s === "error";
  });

  const task = status?.current_task_id
    ? currentTask.data?.find((item) => item.id === status.current_task_id)
    : undefined;

  const mine = events.filter((event) => event.account_id === account.id).slice(0, 12);

  return (
    <section className="main dashboard-workspace-panel">
      <p className="sub" style={{ marginTop: 0 }}>Tổng quan vận hành - {account.label}.</p>

      {/* G1: Agent Operations Banner */}
      <section className={`ops-banner ${problemAccounts.length > 0 ? "attention" : ""}`}>
        <div className="ops-banner-heading">
          <span className="ops-banner-eyebrow">TRUNG TÂM VẬN HÀNH</span>
          <h3>
            {runningAccounts.length} / {accounts.length} agent đang hoạt động
            {problemAccounts.length > 0
              ? ` · ${problemAccounts.length} cần chú ý`
              : accounts.length > 0
                ? " ổn định"
                : ""}
          </h3>
          <div className="ops-banner-facts">
            <span>
              {status?.auth_state ? AUTH_LABEL[status.auth_state] ?? status.auth_state : "TikTok: chưa rõ trạng thái"}
            </span>
            <span>{problemAccounts.length > 0 ? `${problemAccounts.length} lỗi cần xử lý` : "Không có lỗi nghiêm trọng"}</span>
          </div>
        </div>
        <div className="ops-banner-actions">
          <button
            disabled={focusWindow.isPending}
            onClick={() => focusWindow.mutate()}
          >
            Xem Chrome (VNC)
          </button>
          <button onClick={onOpenInbox}>Mở Tin nhắn</button>
          {!running ? (
            <button
              className="primary"
              disabled={act.isPending}
              onClick={() => act.mutate(() => api.start(account.id, runtime, true))}
            >
              {act.isPending ? "Đang khởi động..." : "Chạy Agent"}
            </button>
          ) : (
            <>
              {status?.paused ? (
                <button
                  className="primary"
                  disabled={act.isPending}
                  onClick={() => act.mutate(() => api.resume(account.id))}
                >
                  Tiếp tục Agent
                </button>
              ) : (
                <button
                  disabled={act.isPending || !connected}
                  onClick={() => act.mutate(() => api.pause(account.id, "operator"))}
                >
                  {act.isPending ? "Đang xử lý..." : "Tạm dừng Agent"}
                </button>
              )}
              <button
                className="danger"
                disabled={act.isPending}
                onClick={() => act.mutate(() => api.stop(account.id))}
              >
                Dừng Agent
              </button>
            </>
          )}
        </div>
      </section>

      {running && !connected && (
        <div className="banner warn">
          Tiến trình đang chạy nhưng control port không trả lời. Agent có thể đang khởi
          động, hoặc đã dừng mà tiến trình chưa thoát hẳn.
        </div>
      )}

      {/* G2: KPI */}
      {summary.data && (
        <div className="stat-cards" style={{ marginBottom: 20 }}>
          <div className="stat-card tint-brand">
            <UsersIcon className="stat-card-decor" width={64} height={64} />
            <div className="stat-card-top">
              <span className="stat-card-eyebrow">Khách hàng</span>
              <span className="stat-card-icon brand"><UsersIcon width={18} height={18} /></span>
            </div>
            <div className="n">{summary.data.customers}</div>
            <div className="t">Tổng số trên workspace này</div>
          </div>
          <div className="stat-card tint-warn">
            <AlertTriangleIcon className="stat-card-decor" width={64} height={64} />
            <div className="stat-card-top">
              <span className="stat-card-eyebrow">Chờ xử lý</span>
              <span className="stat-card-icon warn"><AlertTriangleIcon width={18} height={18} /></span>
            </div>
            <div className="n">{summary.data.escalations_open}</div>
            <div className="t">Hội thoại cần người can thiệp</div>
          </div>
          <div className="stat-card tint-info">
            <MessageIcon className="stat-card-decor" width={64} height={64} />
            <div className="stat-card-top">
              <span className="stat-card-eyebrow">Tin nhắn</span>
              <span className="stat-card-icon info"><MessageIcon width={18} height={18} /></span>
            </div>
            <div className="n">{summary.data.messages}</div>
            <div className="t">Đã trao đổi trên workspace này</div>
          </div>
          <div className="stat-card tint-success">
            <CheckCircleIcon className="stat-card-decor" width={64} height={64} />
            <div className="stat-card-top">
              <span className="stat-card-eyebrow">Hoàn thành</span>
              <span className="stat-card-icon success"><CheckCircleIcon width={18} height={18} /></span>
            </div>
            <div className="n">{summary.data.tasks_completed}</div>
            <div className="t">Task đã chạy xong</div>
          </div>
        </div>
      )}

      <div className="grid" style={{ marginBottom: 20 }}>
        {/* G3: Workspace status */}
        <section className="panel">
          <h3>Trạng thái workspace</h3>
          <div className="kv">
            <span className="k">Trạng thái</span>
            <span className="v prose">
              <span className={`dot ${state}`} style={{ marginRight: 6 }} />
              {WORKSPACE_STATE_LABEL[state]}
            </span>
          </div>
          {(state === "sleeping" || state === "waking") && (
            <div className="kv">
              <span className="k">Chrome</span>
              <span className="v prose">
                {state === "sleeping"
                  ? "Đã tắt để tiết kiệm RAM - agent vẫn chạy, sẽ tự bật lại"
                  : "Đang khởi động lại..."}
              </span>
            </div>
          )}
          {account.lifecycle_state !== "sleeping" && account.idle_seconds != null && (
            <div className="kv">
              <span className="k">Không hoạt động</span>
              <span className="v prose">{Math.round(account.idle_seconds / 60)} phút</span>
            </div>
          )}
          <div className="kv">
            <span className="k">Giữ thức (chặn tự sleep)</span>
            <span className="v prose">
              <button
                type="button"
                className={`ws-toggle ${account.pinned ? "on" : ""}`}
                disabled={setPinned.isPending}
                role="switch"
                aria-checked={account.pinned}
                aria-label={account.pinned ? "Bỏ giữ thức" : "Giữ thức workspace này"}
                onClick={() => setPinned.mutate(!account.pinned)}
              >
                <span className="ws-toggle-knob" />
              </button>
            </span>
          </div>
          <div className="kv">
            <span className="k">Thời gian chạy</span>
            <span className="v prose">{uptime(status?.uptime_seconds)}</span>
          </div>
          {SHOW_TECHNICAL_INFO && isAdmin && (
            <button className="advanced-toggle" onClick={() => setAdvancedOpen((v) => !v)}>
              {advancedOpen ? "Ẩn" : "Xem"} chi tiết kỹ thuật
            </button>
          )}
          {SHOW_TECHNICAL_INFO && isAdmin && advancedOpen && (
            <div className="advanced-details">
              <div className="kv">
                <span className="k">Môi trường chạy</span>
                <span className="v prose">{runtimeLabel(status?.runtime ?? account.processes.agent.runtime)}</span>
              </div>
              <div className="kv">
                <span className="k">Cơ sở dữ liệu</span>
                <span className={`v prose ${account.database.ok ? "" : "text-bad"}`}>
                  {account.database.ok ? "Kết nối tốt" : account.database.detail}
                </span>
              </div>
              <div className="kv"><span className="k">account_id</span><span className="v"><code>{account.id}</code></span></div>
              <div className="kv"><span className="k">CDP port</span><span className="v">{account.cdp_port}</span></div>
              <div className="kv"><span className="k">Control port</span><span className="v">{account.control_port}</span></div>
              {/* Shown for the same reason as the two above: it is what an
                  admin needs for the fallback path when the dashboard itself
                  is broken (ssh -L straight to this port - deploy/README.md).
                  Everyday viewing goes through /vnc/<id>/ and never touches
                  it, so this is a debugging affordance, not a link. */}
              <div className="kv"><span className="k">noVNC port</span><span className="v">{account.novnc_port}</span></div>
              <div className="kv"><span className="k">Đường dẫn cấu hình</span><span className="v" style={{ wordBreak: "break-all" }}><code>{account.config_path}</code></span></div>
              {health.data && Object.entries(health.data.checks).map(([name, check]) => (
                <div className="health" key={name}>
                  <span className={`dot ${check.ok ? "running" : "error"}`} />
                  <span className="name">{name}</span>
                  <span className="detail">{check.detail}</span>
                </div>
              ))}
            </div>
          )}
        </section>

        {/* G4: Current task */}
        <section className="panel">
          <h3>Task hiện tại</h3>
          {!running || !status?.current_task_id ? (
            <p className="empty">Agent đang chờ task mới.</p>
          ) : (
            <>
              <div className="kv">
                <span className="k">Việc đang làm</span>
                <span className="v prose">{capabilityLabel(task?.capability)}</span>
              </div>
              <div className="kv">
                <span className="k">Màn hình</span>
                <span className="v prose">{status.current_screen ?? "—"}</span>
              </div>
              <div className="kv">
                <span className="k">Action cuối</span>
                <span className="v prose">{status.last_action ?? "—"}</span>
              </div>
              <div className="kv">
                <span className="k">Số action đã chạy</span>
                <span className="v">{status.action_count ?? "—"}</span>
              </div>
            </>
          )}
        </section>
      </div>

      <section className="panel dark">
        <h3>Sự kiện gần đây</h3>
        <div className="feed">
          {mine.length === 0 && <p className="empty">Chưa có sự kiện</p>}
          {mine.map((event, index) => (
            <div className="row" key={`${event.ts}-${index}`}>
              <span className="time">{new Date(event.ts).toLocaleTimeString("vi-VN")}</span>
              <span className="what">
                <strong>{eventLabel(event)}</strong>
              </span>
            </div>
          ))}
        </div>
      </section>
    </section>
  );
}

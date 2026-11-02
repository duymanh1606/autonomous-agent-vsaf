import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Account, AgentEvent, AgentStatus, Escalation } from "../lib/api";
import { ApiError, api, workspaceState } from "../lib/api";
import {
  AlertTriangleIcon, EyeIcon, EyeOffIcon, FolderIcon, GlobeIcon, KeyIcon, LayersIcon,
  PencilIcon, PlusIcon, PowerIcon, TrashIcon,
} from "../components/ui/icons";
import { DeleteWorkspaceModal } from "../components/Sidebar";
import { HomePage } from "./HomePage";
import { Modal } from "../components/Modal";

// Ẩn thông tin kỹ thuật (id, đường dẫn profile, trạng thái kết nối) khỏi
// mọi người dùng, kể cả admin - đổi thành true để hiện lại cho admin.
const SHOW_TECHNICAL_INFO = false;

function initialsOf(name: string) {
  const letters = name.split(" ").map((part) => part[0]).filter(Boolean).slice(0, 2);
  return (letters.join("") || "??").toUpperCase();
}

type Props = {
  accounts: Account[];
  statuses: Record<string, AgentStatus | undefined>;
  selected: string | null;
  onSelect: (id: string | null) => void;
  onCreate: (label: string) => void;
  onUpdate: (id: string, fields: { label?: string; api_key?: string }) => Promise<void>;
  onDelete: (id: string, purge: boolean) => Promise<void>;
  escalationsByAccount: Record<string, Escalation[] | undefined>;
  alertCount: number;
  events: AgentEvent[];
  onOpenInbox: () => void;
  isAdmin: boolean;
  /** Whether to offer the create button at all: an admin, or a user whose
   * `max_workspaces` quota is above zero. Someone the admin has granted no
   * quota is not shown a button that would only ever 403. */
  mayCreate: boolean;
};

export function Dashboard({
  accounts, statuses, selected, onSelect, onCreate, onUpdate, onDelete,
  escalationsByAccount, alertCount, events, onOpenInbox, isAdmin, mayCreate,
}: Props) {
  const client = useQueryClient();
  const [createOpen, setCreateOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Account | null>(null);
  const [editTarget, setEditTarget] = useState<Account | null>(null);

  // One toggle mutation shared by every card - the pending row is told apart
  // by comparing its account id against the mutation's own in-flight variables.
  const toggle = useMutation({
    mutationFn: ({ account, turnOn }: { account: Account; turnOn: boolean }) =>
      turnOn ? api.start(account.id, "langgraph", true) : api.stop(account.id),
    onSuccess: () => client.invalidateQueries({ queryKey: ["accounts"] }),
  });

  const runningCount = accounts.filter((item) => item.processes.agent.running).length;
  const selectedAccount = accounts.find((item) => item.id === selected);

  const renderCard = (account: Account) => {
    const isSelected = account.id === selected;
    const state = workspaceState(account, statuses[account.id]);
    // "On" for the toggle means something is running that Stop would end -
    // a Chrome left behind by an agent that crashed at boot included.
    const running = account.processes.agent.running || account.processes.chrome.running;
    const pending = toggle.isPending && toggle.variables?.account.id === account.id;
    const escalationCount = escalationsByAccount[account.id]?.length ?? 0;

    return (
      <article
        key={account.id}
        className={`workspace-card ${isSelected ? "selected" : ""}`}
        onClick={() => onSelect(isSelected ? null : account.id)}
      >
        <div className="workspace-card-top">
          <div className="workspace-card-avatar">{initialsOf(account.label)}</div>
          <div className="workspace-card-heading">
            <div className="workspace-card-name-row">
              <span className="workspace-card-name">{account.label}</span>
              {isSelected && (
                <span className="workspace-card-selected-badge" title="Bấm lại để bỏ chọn">
                  Đang chọn
                </span>
              )}
            </div>
            {SHOW_TECHNICAL_INFO && <div className="workspace-card-handle">@{account.id}</div>}
          </div>
          <button
            type="button"
            className={`ws-toggle ${running ? "on" : ""}`}
            disabled={pending}
            role="switch"
            aria-checked={running}
            aria-label={running ? "Tắt agent" : "Bật agent"}
            onClick={(event) => {
              event.stopPropagation();
              toggle.mutate({ account, turnOn: !running });
            }}
          >
            <span className="ws-toggle-knob" />
          </button>
        </div>

        <div className="workspace-card-info">
          <div className="workspace-card-info-row">
            <KeyIcon width={14} height={14} />
            <span>{account.has_api_key ? "Đã cấu hình API key" : "Chưa cấu hình API key"}</span>
          </div>
          {account.egw_active && (
            <div className="workspace-card-info-row" title="Workspace này đi ra Internet bằng IP riêng qua egress gateway">
              <GlobeIcon width={14} height={14} />
              <span className="workspace-card-info-value">
                IP riêng{account.egw_expected_ip ? `: ${account.egw_expected_ip}` : " (token nhập tay)"}
              </span>
            </div>
          )}
          {SHOW_TECHNICAL_INFO && (
            <div className="workspace-card-info-row">
              <FolderIcon width={14} height={14} />
              <span className="workspace-card-info-value">
                {account.profile_dir_name || account.profile_dir}
              </span>
            </div>
          )}
        </div>

        <div className="workspace-card-meta">
          <span>{escalationCount} cảnh báo cần xử lý</span>
          {SHOW_TECHNICAL_INFO && <span>{account.control_connected ? "Đã kết nối" : "Chưa kết nối"}</span>}
        </div>

        <div className="workspace-card-footer">
          <span className={`workspace-card-state ${running ? "on" : "off"}`}>
            <span className={`dot ${state}`} />
            {account.processes.agent.running
              ? "Agent đang hoạt động"
              : state === "chrome_only"
                ? "Chrome đang mở, agent đã tắt"
                : "Agent đang tắt"}
          </span>
          <div className="workspace-card-footer-actions">
            <button
              type="button"
              className="workspace-card-icon-btn"
              aria-label={`Sửa workspace ${account.label}`}
              title="Sửa tên / API key"
              onClick={(event) => {
                event.stopPropagation();
                setEditTarget(account);
              }}
            >
              <PencilIcon width={13} height={13} />
            </button>
            <button
              type="button"
              className="workspace-card-delete"
              aria-label={`Xoá workspace ${account.label}`}
              onClick={(event) => {
                event.stopPropagation();
                setDeleteTarget(account);
              }}
            >
              <TrashIcon width={13} height={13} />
            </button>
          </div>
        </div>
      </article>
    );
  };

  return (
    <main className="main">
      <div className="dashboard-head">
        <div>
          <h2>Tổng quan</h2>
          <p className="sub" style={{ marginBottom: 0 }}>
            {accounts.length} workspace · {runningCount} đang bật
          </p>
        </div>
        {mayCreate && (
          // Admins always; everyone else only within the quota an admin
          // granted them (Người dùng → Giới hạn workspace). The API enforces
          // it either way - this keeps the UI from offering a button that
          // would always 403.
          <button className="primary" onClick={() => setCreateOpen(true)}>
            <PlusIcon width={15} height={15} style={{ marginRight: 6, verticalAlign: -3 }} />
            Tạo workspace mới
          </button>
        )}
      </div>

      <div className="stat-cards" style={{ marginBottom: 14 }}>
        <div className="stat-card tint-brand">
          <LayersIcon className="stat-card-decor" width={64} height={64} />
          <div className="stat-card-top">
            <span className="stat-card-eyebrow">Tổng workspace</span>
            <span className="stat-card-icon brand"><LayersIcon width={18} height={18} /></span>
          </div>
          <div className="n">{accounts.length}</div>
          <div className="t">Đang quản lý trên hệ thống</div>
        </div>

        <div className="stat-card tint-success">
          <PowerIcon className="stat-card-decor" width={64} height={64} />
          <div className="stat-card-top">
            <span className="stat-card-eyebrow">Agent đang bật</span>
            <span className="stat-card-icon success"><PowerIcon width={18} height={18} /></span>
          </div>
          <div className="n">
            {runningCount}
            <span style={{ fontSize: 15, fontWeight: 600, color: "var(--muted)", marginLeft: 4 }}>
              / {accounts.length}
            </span>
          </div>
          <div className="stat-card-bar">
            <div
              className="stat-card-bar-fill"
              style={{ width: `${accounts.length ? (runningCount / accounts.length) * 100 : 0}%` }}
            />
          </div>
        </div>

        <div className="stat-card tint-warn">
          <AlertTriangleIcon className="stat-card-decor" width={64} height={64} />
          <div className="stat-card-top">
            <span className="stat-card-eyebrow">Cảnh báo cần xử lý</span>
            <span className="stat-card-icon warn"><AlertTriangleIcon width={18} height={18} /></span>
          </div>
          <div className="n">{alertCount}</div>
          <div className="t">Trên toàn bộ workspace</div>
        </div>
      </div>

      {accounts.length === 0 ? (
        <p className="empty">
          {mayCreate
            ? "Chưa có workspace nào. Tạo workspace đầu tiên để bắt đầu chạy agent."
            : "Bạn chưa được cấp quyền vào workspace nào. Liên hệ quản trị viên để được cấp quyền hoặc được cấp hạn mức tạo workspace."}
        </p>
      ) : (
        <div className="workspace-grid">{accounts.map(renderCard)}</div>
      )}

      {selectedAccount ? (
        <div className="dashboard-workspace-detail">
          <h3 className="dashboard-section-title">Vận hành workspace đang chọn</h3>
          <HomePage
            account={selectedAccount}
            accounts={accounts}
            statuses={statuses}
            events={events}
            onOpenInbox={onOpenInbox}
            isAdmin={isAdmin}
          />
        </div>
      ) : accounts.length > 0 ? (
        <p className="empty" style={{ marginTop: 20 }}>
          Chưa chọn workspace nào - bấm vào một thẻ ở trên để xem chi tiết vận hành.
        </p>
      ) : null}

      {createOpen && (
        <CreateWorkspaceModal
          onClose={() => setCreateOpen(false)}
          onCreate={(label) => {
            onCreate(label);
            setCreateOpen(false);
          }}
        />
      )}

      {editTarget && (
        <EditWorkspaceModal
          account={editTarget}
          onClose={() => setEditTarget(null)}
          onSave={async (fields) => {
            await onUpdate(editTarget.id, fields);
            setEditTarget(null);
          }}
        />
      )}

      {deleteTarget && (
        <DeleteWorkspaceModal
          account={deleteTarget}
          running={deleteTarget.processes.agent.running || deleteTarget.processes.chrome.running}
          onCancel={() => setDeleteTarget(null)}
          onConfirm={async (purge) => {
            await onDelete(deleteTarget.id, purge);
            setDeleteTarget(null);
          }}
        />
      )}
    </main>
  );
}

function CreateWorkspaceModal({
  onClose, onCreate,
}: {
  onClose: () => void;
  onCreate: (label: string) => void;
}) {
  const [label, setLabel] = useState("");
  const canSubmit = label.trim().length > 0;

  const submit = () => {
    if (!canSubmit) return;
    // Workspace id, Chrome profile directory and API key are all handled
    // afterward: id is generated by the backend, the profile directory
    // defaults to that id, and the API key can be filled in later via
    // "Sửa" - Create only ever asks for the one thing an operator actually
    // has to decide right now, the display name.
    onCreate(label.trim());
  };

  return (
    <Modal onClose={onClose} title="Tạo workspace mới" style={{ maxWidth: 440 }}>
        <div className="product-form-header">
          <h3>Tạo workspace mới</h3>
          <button onClick={onClose}>Đóng</button>
        </div>
        <p className="sub" style={{ marginTop: -6 }}>
          Sau khi tạo, Chrome sẽ tự mở để bạn đăng nhập TikTok Shop lần đầu - phiên
          đăng nhập được giữ lại cho những lần sau. API key có thể điền sau bằng nút
          "Sửa" trên thẻ workspace.
        </p>

        <label className="product-form-field">
          <span>Tên hiển thị</span>
          <input
            placeholder="vd: Sóng Beauty Store"
            value={label}
            onChange={(event) => setLabel(event.target.value)}
            onKeyDown={(event) => event.key === "Enter" && submit()}
            autoFocus
          />
        </label>

        <div className="product-form-footer">
          <button onClick={onClose}>Huỷ</button>
          <button className="primary" disabled={!canSubmit} onClick={submit}>Tạo workspace</button>
        </div>
    </Modal>
  );
}

function EditWorkspaceModal({
  account, onClose, onSave,
}: {
  account: Account;
  onClose: () => void;
  onSave: (fields: { label?: string; api_key?: string }) => Promise<void>;
}) {
  const [label, setLabel] = useState(account.label);
  // Never pre-filled - the real key never reaches the frontend (see
  // dashboard_api/accounts.py's Account.as_dict). Left blank means "leave
  // the existing key untouched"; typing something replaces it.
  const [apiKey, setApiKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [saving, setSaving] = useState(false);
  const canSubmit = label.trim().length > 0;

  const submit = async () => {
    if (!canSubmit || saving) return;
    setSaving(true);
    try {
      const fields: { label?: string; api_key?: string } = {};
      if (label.trim() !== account.label) fields.label = label.trim();
      if (apiKey.trim()) fields.api_key = apiKey.trim();
      await onSave(fields);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal onClose={onClose} title="Sửa workspace" style={{ maxWidth: 440 }}>
        <div className="product-form-header">
          <h3>Sửa workspace</h3>
          <button onClick={onClose}>Đóng</button>
        </div>

        <label className="product-form-field">
          <span>Tên hiển thị</span>
          <input
            value={label}
            onChange={(event) => setLabel(event.target.value)}
            autoFocus
          />
        </label>
        <label className="product-form-field">
          <span>
            Gemini API key{" "}
            {account.has_api_key ? "(đã cấu hình - để trống nếu không đổi)" : "(chưa cấu hình)"}
          </span>
          <div className="product-form-key-row">
            <input
              type={showKey ? "text" : "password"}
              placeholder="AIza..."
              value={apiKey}
              onChange={(event) => setApiKey(event.target.value)}
              onKeyDown={(event) => event.key === "Enter" && submit()}
            />
            <button
              type="button"
              className="product-form-key-toggle"
              onClick={() => setShowKey((value) => !value)}
              aria-label={showKey ? "Ẩn API key" : "Hiện API key"}
            >
              {showKey ? <EyeOffIcon width={15} height={15} /> : <EyeIcon width={15} height={15} />}
            </button>
          </div>
        </label>
        <p className="sub" style={{ marginTop: -6, fontSize: 12 }}>
          Nếu workspace đang chạy, cần Dừng rồi Chạy lại agent để áp dụng API key mới.
        </p>

        <EgwSection account={account} />

        <div className="product-form-footer">
          <button onClick={onClose}>Huỷ</button>
          <button className="primary" disabled={!canSubmit || saving} onClick={submit}>
            {saving ? "Đang lưu..." : "Lưu"}
          </button>
        </div>
    </Modal>
  );
}

// One public IP per workspace through the egress gateway. Renders nothing at
// all on a machine without egw (`configured: false`) - the feature is then
// simply absent rather than shown broken. Saves immediately (its own
// endpoints, not part of the modal's Save button) because enabling means a
// round-trip to egw that either succeeds or explains why not.
function EgwSection({ account }: { account: Account }) {
  const client = useQueryClient();
  const routes = useQuery({ queryKey: ["egw-routes"], queryFn: api.egwRoutes, staleTime: 15_000 });
  const [route, setRoute] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = () => {
    client.invalidateQueries({ queryKey: ["accounts"] });
    client.invalidateQueries({ queryKey: ["egw-routes"] });
  };
  const fail = (err: unknown) =>
    setError(err instanceof ApiError || err instanceof Error ? err.message : String(err));

  const enable = useMutation({
    mutationFn: (routeId: string) => api.enableEgw(account.id, routeId),
    onSuccess: (result) => {
      setError(null);
      setNotice(
        `Đã bật IP riêng ${result.egw_expected_ip} (${result.egw_route}).` +
          (result.restart_required ? " Dừng rồi Chạy lại workspace để áp dụng." : ""),
      );
      refresh();
    },
    onError: fail,
  });
  const disable = useMutation({
    mutationFn: () => api.disableEgw(account.id),
    onSuccess: (result) => {
      setError(null);
      setNotice(
        "Đã gỡ IP riêng - workspace sẽ dùng IP của máy chủ." +
          (result.restart_required ? " Dừng rồi Chạy lại workspace để áp dụng." : ""),
      );
      refresh();
    },
    onError: fail,
  });

  if (routes.isPending || !routes.data?.configured) return null;

  const busy = enable.isPending || disable.isPending;
  const free = routes.data.routes.filter(
    (r) => r.enabled && r.status === "HEALTHY" &&
      (r.held_by.length === 0 || r.held_by.every((id) => id === account.id)),
  );
  const current = account.has_egw_token
    ? account.egw_expected_ip
      ? `${account.egw_expected_ip} (${account.egw_route})`
      : "token nhập tay"
    : null;

  return (
    <div className="product-form-field" style={{ borderTop: "1px solid var(--border)", paddingTop: 12 }}>
      <span>
        IP riêng qua egress gateway{" "}
        {current ? `(đang bật: ${current})` : "(chưa bật - dùng IP của máy chủ)"}
      </span>
      <div className="product-form-key-row">
        <select
          value={route}
          disabled={busy}
          onChange={(event) => setRoute(event.target.value)}
          aria-label="Chọn route egw"
        >
          <option value="">{free.length ? "Chọn IP..." : "Không còn IP trống (HEALTHY)"}</option>
          {free.map((r) => (
            <option key={r.id} value={r.id}>
              {r.source_ip} — {r.id}{r.held_by.includes(account.id) ? " (đang dùng)" : ""}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="primary"
          disabled={busy || !route}
          onClick={() => enable.mutate(route)}
        >
          {enable.isPending ? "Đang bật..." : current ? "Đổi IP" : "Bật"}
        </button>
        {account.has_egw_token && (
          <button type="button" disabled={busy} onClick={() => disable.mutate()}>
            {disable.isPending ? "Đang gỡ..." : "Gỡ"}
          </button>
        )}
      </div>
      {error && <p className="sub" style={{ color: "var(--danger, #c0392b)", fontSize: 12, marginTop: 4 }}>{error}</p>}
      {notice && !error && <p className="sub" style={{ fontSize: 12, marginTop: 4 }}>{notice}</p>}
      {!error && !notice && (
        <p className="sub" style={{ fontSize: 12, marginTop: 4 }}>
          Mỗi shop một IP. Chỉ liệt kê IP đang HEALTHY và chưa shop nào dùng. Có hiệu lực khi
          Chạy lại workspace.
        </p>
      )}
    </div>
  );
}

import { useEffect, useRef, useState } from "react";
import type { Account, AgentStatus } from "../lib/api";
import { WORKSPACE_STATE_LABEL, workspaceState } from "../lib/api";
import type { Page } from "../App";
import {
  ActivityIcon, AlertTriangleIcon, ChevronDownIcon, FileTextIcon, GridIcon, InboxIcon,
  MessageIcon, PackageIcon, PowerIcon, ReceiptIcon, SlidersIcon, TrashIcon,
  UsersIcon,
} from "./ui/icons";
import { Modal } from "./Modal";

const STATE_LABEL = WORKSPACE_STATE_LABEL;

type NavGroup = { label: string; items: { page: Page; label: string; icon: (props: { className?: string }) => JSX.Element; badge?: number }[] };

type Props = {
  accounts: Account[];
  statuses: Record<string, AgentStatus | undefined>;
  selected: string | null;
  connection: string;
  onSelect: (id: string) => void;
  onCreate: (label: string) => void;
  // Resolves/rejects so the confirm modal can show the real backend error
  // (e.g. the 409 "still running elsewhere" refusal) instead of guessing.
  onDelete: (id: string, purge: boolean) => Promise<void>;
  onStart: (id: string) => void;
  onStop: (id: string) => void;
  /** The workspace a start/stop is in flight for, so the button can say so
   * instead of looking unresponsive while Chrome boots or winds down. */
  busyId: string | null;
  page: Page;
  onNavigate: (page: Page) => void;
  alertCount: number;
  isAdmin: boolean;
  /** Non-admin workspace quota (dashboard_api users.max_workspaces): how
   * many they may create vs. how many they already own. null = uncapped,
   * which is admins only - a non-admin an admin has granted nothing has a
   * quota of 0, not a missing one. */
  createLimit?: { max: number; owned: number } | null;
};

export function Sidebar({
  accounts, statuses, selected, connection, onSelect, onCreate, onDelete,
  onStart, onStop, busyId, page, onNavigate, alertCount, isAdmin,
  createLimit = null,
}: Props) {
  // A quota of 0 is not "at the limit", it is "never offered": showing a
  // permanently disabled button to every member of staff is noise, and the
  // empty state already tells them to ask an admin for access.
  const mayCreate = isAdmin || (createLimit !== null && createLimit.max > 0);
  const atLimit = createLimit !== null && createLimit.owned >= createLimit.max;
  const [switcherOpen, setSwitcherOpen] = useState(false);
  const [adding, setAdding] = useState(false);
  const [label, setLabel] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<Account | null>(null);
  const switcherRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!switcherOpen) return;
    const onDocClick = (event: MouseEvent) => {
      if (switcherRef.current && !switcherRef.current.contains(event.target as Node)) {
        setSwitcherOpen(false);
        setAdding(false);
      }
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setSwitcherOpen(false);
        setAdding(false);
      }
    };
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [switcherOpen]);

  const account = accounts.find((item) => item.id === selected) ?? null;
  const state = account ? workspaceState(account, statuses[account.id]) : "idle";
  // "idle" is the only state Start applies to. A sleeping workspace wakes on
  // its own, and everything else is either already up or mid-transition -
  // offering Start there would just be a button that does nothing.
  const canStart = state === "idle" || state === "chrome_only";
  // Anything with a live process can be stopped - the agent, or a Chrome
  // whose agent died at boot. Keying this on the agent alone is what left
  // an orphaned Chrome with no button to close it.
  const canStop = Boolean(
    account?.processes.agent.running || account?.processes.chrome.running,
  );
  const busy = busyId === account?.id;

  // Stopping takes a shop offline mid-conversation, and this button sits one
  // stray click away in a compact sidebar. So it arms on the first click and
  // acts on the second; the arm times out on its own so a change of mind
  // never leaves a live "confirm" waiting to be hit by accident later.
  const [stopArmed, setStopArmed] = useState(false);
  useEffect(() => {
    if (!stopArmed) return;
    const timer = setTimeout(() => setStopArmed(false), 4000);
    return () => clearTimeout(timer);
  }, [stopArmed]);
  useEffect(() => {
    setStopArmed(false);
  }, [selected]);

  const submit = () => {
    const trimmedLabel = label.trim();
    if (!trimmedLabel) return;
    // Workspace id/Chrome profile are derived server-side, and Chrome opens
    // right away for first login (App.tsx's create mutation) - API key can
    // be added later from the workspace card's "Sửa" button.
    onCreate(trimmedLabel);
    setLabel("");
    setAdding(false);
    setSwitcherOpen(false);
  };

  const NAV: NavGroup[] = [
    {
      label: "QUẢN LÝ",
      items: [
        { page: "home", label: "Tổng quan", icon: GridIcon },
        { page: "inbox", label: "Tin nhắn", icon: InboxIcon },
        { page: "orders", label: "Đơn hàng", icon: ReceiptIcon },
        { page: "products", label: "Sản phẩm", icon: PackageIcon },
        { page: "automation", label: "Tự động hóa", icon: SlidersIcon },
        { page: "scenario", label: "Kịch bản", icon: MessageIcon },
      ],
    },
    {
      label: "VẬN HÀNH",
      items: [
        { page: "activity", label: "Hoạt động", icon: ActivityIcon },
        { page: "alerts", label: "Cảnh báo", icon: AlertTriangleIcon, badge: alertCount },
        ...(isAdmin
          ? [
              { page: "users" as Page, label: "Người dùng", icon: UsersIcon },
              { page: "logs" as Page, label: "Nhật ký hệ thống", icon: FileTextIcon },
            ]
          : []),
      ],
    },
  ];

  return (
    <aside className="sidebar">
      <header>
        <h1>Agent TIKTOK</h1>
        <span
          className={`tag ${connection === "open" ? "ok" : "bad"}`}
          title="Kết nối tới supervisor"
        >
          {connection === "open" ? "trực tuyến" : "mất kết nối"}
        </span>
      </header>

      <div className="workspace-switcher" ref={switcherRef}>
        <div className="workspace-switcher-current">
          <button
            className="workspace-switcher-trigger"
            aria-haspopup="listbox"
            aria-expanded={switcherOpen}
            aria-label={account ? `Workspace: ${account.label}. Chọn workspace khác` : "Chọn workspace"}
            onClick={() => setSwitcherOpen((value) => !value)}
          >
            {account ? (
              <>
                <span className={`dot ${state}`} />
                <span className="workspace-switcher-meta">
                  <span className="workspace-switcher-name">{account.label}</span>
                  <span className="workspace-switcher-state">{STATE_LABEL[state]}</span>
                </span>
              </>
            ) : (
              <span className="workspace-switcher-meta">
                <span className="workspace-switcher-name">Chưa có workspace</span>
              </span>
            )}
            <ChevronDownIcon className="workspace-switcher-chevron" />
          </button>

          {/* Start and Stop for the selected workspace, without a trip to
              Dashboard. Start is one click. Stop is two - see `stopArmed`. */}
          {account && canStart && (
            <button
              className="workspace-switcher-start"
              disabled={busy}
              onClick={() => onStart(account.id)}
              aria-label={`Khởi động ${account.label}`}
            >
              <PowerIcon width={13} height={13} />
              {busy ? "Đang khởi động..." : "Khởi động workspace"}
            </button>
          )}
          {account && canStop && (
            <button
              className={`workspace-switcher-stop ${stopArmed ? "armed" : ""}`}
              disabled={busy}
              onClick={() => {
                if (!stopArmed) {
                  setStopArmed(true);
                  return;
                }
                setStopArmed(false);
                onStop(account.id);
              }}
              aria-label={stopArmed ? `Bấm lần nữa để dừng ${account.label}` : `Dừng ${account.label}`}
            >
              <PowerIcon width={13} height={13} />
              {busy ? "Đang dừng..." : stopArmed ? "Bấm lần nữa để dừng" : "Dừng workspace"}
            </button>
          )}
        </div>

        {switcherOpen && (
          <div className="workspace-switcher-panel" role="listbox" aria-label="Danh sách workspace">
            {!adding ? (
              <>
                {accounts.map((item) => {
                  const itemState = workspaceState(item, statuses[item.id]);
                  return (
                    <div className="workspace-switcher-row" key={item.id}>
                      <button
                        className={`workspace-switcher-item ${item.id === selected ? "active" : ""}`}
                        role="option"
                        aria-selected={item.id === selected}
                        onClick={() => {
                          onSelect(item.id);
                          setSwitcherOpen(false);
                        }}
                      >
                        <span className={`dot ${itemState}`} />
                        <span className="workspace-switcher-item-meta">
                          <span className="workspace-switcher-item-name">{item.label}</span>
                          <span className="workspace-switcher-item-state">{STATE_LABEL[itemState]}</span>
                        </span>
                      </button>
                      <button
                        className="workspace-switcher-delete"
                        title={`Xoá workspace ${item.label}`}
                        aria-label={`Xoá workspace ${item.label}`}
                        onClick={(event) => {
                          event.stopPropagation();
                          setDeleteTarget(item);
                        }}
                      >
                        <TrashIcon width={14} height={14} />
                      </button>
                    </div>
                  );
                })}
                {accounts.length === 0 && (
                  <p className="workspace-switcher-empty">Chưa có workspace nào.</p>
                )}
                {mayCreate && (
                  <button
                    className="workspace-switcher-add"
                    disabled={atLimit}
                    title={
                      atLimit
                        ? `Đã dùng hết ${createLimit!.max} workspace được cấp - liên hệ admin để nâng giới hạn`
                        : undefined
                    }
                    onClick={() => setAdding(true)}
                  >
                    + Thêm workspace
                    {createLimit !== null && ` (${createLimit.owned}/${createLimit.max})`}
                  </button>
                )}
              </>
            ) : (
              <div className="workspace-switcher-form">
                <input
                  placeholder="tên hiển thị (vd: Shop Chính)"
                  value={label}
                  onChange={(event) => setLabel(event.target.value)}
                  onKeyDown={(event) => event.key === "Enter" && submit()}
                  autoFocus
                />
                <div style={{ display: "flex", gap: 6 }}>
                  <button className="primary" onClick={submit} style={{ flex: 1 }}>
                    Tạo
                  </button>
                  <button onClick={() => setAdding(false)}>Huỷ</button>
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      <nav className="rail-nav" aria-label="Điều hướng chính">
        {NAV.map((group) => (
          <div key={group.label} className="rail-nav-section">
            <div className="rail-nav-label">{group.label}</div>
            {group.items.map((item) => (
              <button
                key={item.page}
                className={`rail-nav-item ${page === item.page ? "active" : ""}`}
                onClick={() => onNavigate(item.page)}
              >
                <item.icon className="rail-nav-icon" />
                <span className="rail-nav-item-label">{item.label}</span>
                {Boolean(item.badge) && <span className="rail-nav-badge">{item.badge}</span>}
              </button>
            ))}
          </div>
        ))}
      </nav>

      <footer>
        <div className="sidebar-agent-status">
          <span className={`dot ${state}`} />
          <span>{account ? `${account.label} · ${STATE_LABEL[state]}` : "Chưa chọn workspace"}</span>
        </div>
        <div className="sidebar-version">PHIÊN BẢN AGENT TIKTOK · 08.26</div>
      </footer>

      {deleteTarget && (
        <DeleteWorkspaceModal
          account={deleteTarget}
          running={
            deleteTarget.processes.agent.running || deleteTarget.processes.chrome.running
          }
          onCancel={() => setDeleteTarget(null)}
          onConfirm={async (purge) => {
            await onDelete(deleteTarget.id, purge);
            setDeleteTarget(null);
          }}
        />
      )}
    </aside>
  );
}

export function DeleteWorkspaceModal({
  account, running, onCancel, onConfirm,
}: {
  account: Account;
  running: boolean;
  onCancel: () => void;
  onConfirm: (purge: boolean) => Promise<void>;
}) {
  const [confirmText, setConfirmText] = useState("");
  const [purge, setPurge] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const canDelete = confirmText.trim() === account.id;

  const submit = async () => {
    if (!canDelete || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onConfirm(purge);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Xoá workspace thất bại.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal onClose={onCancel} title={`Xoá workspace ${account.label}`} className="danger-modal" overlayClassName="danger-modal-overlay">
        <h3>Xoá workspace "{account.label}"?</h3>

        {running && (
          <div className="banner warn">
            Agent (và/hoặc Chrome) của workspace này đang chạy. Xoá sẽ tự động dừng
            hẳn trước khi gỡ, giống hệt nút "Dừng Agent".
          </div>
        )}

        <p className="sub" style={{ marginTop: 0 }}>Sẽ bị gỡ khỏi danh sách:</p>
        <ul className="danger-modal-list">
          <li>File cấu hình (<code>configs/accounts/{account.id}.yaml</code>)</li>
          <li>Workspace này khỏi danh sách quản lý</li>
        </ul>
        <p className="sub">
          {purge ? "Sẽ bị xoá luôn vì bạn đã chọn \"xoá sạch\" bên dưới:" : "Vẫn được giữ nguyên trên đĩa (không bị xoá):"}
        </p>
        <ul className="danger-modal-list">
          <li>Chrome profile / cookie / phiên đăng nhập TikTok</li>
          <li>Sản phẩm và media đã upload (<code>data/accounts/{account.id}/</code>)</li>
        </ul>
        <p className="sub">Luôn được giữ nguyên (không phụ thuộc tuỳ chọn xoá sạch):</p>
        <ul className="danger-modal-list">
          <li>Lịch sử hội thoại, task, log trong database</li>
        </ul>

        <label className="danger-modal-confirm-label" style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <input
            type="checkbox"
            checked={purge}
            onChange={(event) => setPurge(event.target.checked)}
          />
          Xoá sạch luôn Chrome profile/cookie và thư mục dữ liệu workspace này
        </label>
        {purge && (
          <div className="banner bad">
            Xoá sạch nghĩa là mất luôn phiên đăng nhập TikTok (phải quét lại QR/CAPTCHA
            nếu tạo lại workspace) và mọi sản phẩm/media đã upload. Không thể hoàn tác.
          </div>
        )}

        {error && <div className="banner bad">{error}</div>}

        <label className="danger-modal-confirm-label">
          Gõ chính xác <code>{account.id}</code> để xác nhận:
        </label>
        <input
          value={confirmText}
          onChange={(event) => setConfirmText(event.target.value)}
          placeholder={account.id}
          autoFocus
        />

        <div className="danger-modal-actions">
          <button onClick={onCancel} disabled={busy}>Huỷ</button>
          <button className="danger" disabled={!canDelete || busy} onClick={submit}>
            {busy ? "Đang xoá..." : purge ? "Xoá sạch workspace" : "Xoá workspace"}
          </button>
        </div>
    </Modal>
  );
}

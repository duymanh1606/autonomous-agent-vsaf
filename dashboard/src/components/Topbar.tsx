import { useState } from "react";
import { BellIcon, SearchIcon } from "./ui/icons";
import { AccountDialog, avatarInitial } from "./AccountDialog";
import type { User } from "../lib/api";

export function Topbar({
  connection,
  notificationCount,
  onOpenAlerts,
  onOpenSearch,
  user,
  onLogout,
}: {
  connection: string;
  notificationCount: number;
  onOpenAlerts: () => void;
  onOpenSearch: () => void;
  user: User;
  onLogout: () => void;
}) {
  const [accountOpen, setAccountOpen] = useState(false);
  return (
    <header className="topbar">
      {/* Just the product identity - which page this is now belongs to
          App.tsx's .page-breadcrumb, right above each page's own title.
          This used to repeat the page name too ("... / Người dùng"), sitting
          directly above that same text again - two labels for one fact,
          easy to stop noticing either. */}
      <span className="breadcrumb">TRUNG TÂM ĐIỀU KHIỂN</span>

      <button className="topbar-search" onClick={onOpenSearch}>
        <SearchIcon />
        <span>Điều hướng...</span>
        <span className="topbar-search-shortcut">⌘K</span>
      </button>

      <div className="topbar-right">
        <span className={`status-chip ${connection === "open" ? "" : "bad"}`}>
          {connection === "open" ? "● Hệ thống ổn định" : "● Mất kết nối"}
        </span>
        <button
          className="topbar-bell"
          title="Cảnh báo"
          aria-label="Cảnh báo"
          onClick={onOpenAlerts}
        >
          <BellIcon />
          {notificationCount > 0 && (
            <span className="topbar-bell-badge">{Math.min(notificationCount, 99)}</span>
          )}
        </button>
        <button
          type="button"
          className="topbar-user"
          title="Tài khoản của tôi"
          aria-label="Tài khoản của tôi"
          onClick={() => setAccountOpen(true)}
        >
          <span className="topbar-avatar-circle" aria-hidden="true">{avatarInitial(user)}</span>
          <span className="topbar-user-name">{user.email}</span>
        </button>
        <button className="topbar-logout" onClick={onLogout}>
          Đăng xuất
        </button>
      </div>
      {accountOpen && <AccountDialog user={user} onClose={() => setAccountOpen(false)} />}
    </header>
  );
}

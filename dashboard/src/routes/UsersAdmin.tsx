// Admin-only user management ("Người dùng" page). Header + 4 KPI stat cards
// (same .stat-card system Dashboard.tsx uses, so this page reads as part of
// the same app instead of a bolted-on screen) + status tabs + one card per
// operator account.
//
// Each card shows only identity (avatar/email), status/role, and workspace
// count - everything else lives behind two dedicated dialogs so the grid
// stays scannable even with many users:
//   * "Chi tiết" (UserDetailModal) - read-mostly: usage, the owned/granted
//     workspace breakdown, and granting/revoking access.
//   * "Chỉnh sửa" (UserEditModal) - account mutations: password, workspace
//     limit, activate/deactivate, delete.
//
// Admin accounts only ever come from `python -m dashboard_api.create_admin`,
// run by hand against the backend - "+ Thêm người dùng" (POST /api/users)
// can only ever create a plain user, never another admin, to keep that a
// single, deliberate path.
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import type { Account, ManagedUser } from "../lib/api";
import {
  CheckCircleIcon, FolderIcon, PlusIcon, UsersIcon,
} from "../components/ui/icons";
import { Modal } from "../components/Modal";
import { NumberInput } from "../components/NumberInput";
import { gmailProblem } from "../lib/email";

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function formatDate(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleDateString("vi-VN");
}

// A plain CSV of what is already loaded - no extra request, no backend
// endpoint to maintain. Quoted per RFC 4180 so an email/label with a comma
// cannot shift columns.
function csvCell(value: string | number): string {
  const text = String(value);
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

function downloadUsersCsv(users: ManagedUser[]): void {
  const header = ["email", "vai_tro", "trang_thai", "tao_ngay", "workspace_duoc_cap",
    "workspace_da_tao", "gioi_han_workspace", "token_gemini"];
  const rows = users.map((u) => [
    u.email, u.is_admin ? "admin" : "user", u.is_active ? "hoat_dong" : "vo_hieu_hoa",
    // Both numbers mean something and they are not the same: how many
    // workspaces this person was let into, and how many of their own quota
    // they have spent. Admins are uncapped, which is not a number.
    u.created_at, u.account_ids.length, u.owned_workspaces,
    u.is_admin ? "khong_gioi_han" : u.max_workspaces ?? 0, u.usage.total_tokens,
  ]);
  const csv = [header, ...rows].map((row) => row.map(csvCell).join(",")).join("\n");
  const url = URL.createObjectURL(new Blob([`﻿${csv}`], { type: "text/csv;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `nguoi-dung-${new Date().toISOString().slice(0, 10)}.csv`;
  link.click();
  URL.revokeObjectURL(url);
}

function StatusPill({ active, pending }: { active: boolean; pending?: boolean }) {
  // Three states, not two: a self-registered account that nobody has approved
  // yet is not the same thing as one an admin switched off, and the first is
  // the one that needs someone to act.
  const label = active ? "Hoạt động" : pending ? "Chờ duyệt" : "Vô hiệu hoá";
  const tone = active ? " ok" : pending ? " warn" : " bad";
  return (
    <span className={`status-pill${tone}`}>
      <span className="status-pill-dot" />
      {label}
    </span>
  );
}

function RolePill({ admin }: { admin: boolean }) {
  return <span className="tag ok">{admin ? "★ Quản trị viên" : "Thành viên"}</span>;
}

// -------------------------------------------------------------- Chi tiết
function UserDetailModal({
  user, accounts, onClose,
}: {
  user: ManagedUser;
  accounts: Account[];
  onClose: () => void;
}) {
  const client = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const invalidate = () => client.invalidateQueries({ queryKey: ["users"] });
  const onError = (err: Error) => setError(err.message);

  const grant = useMutation({
    mutationFn: (accountId: string) => api.grantAccess(user.id, accountId),
    onSuccess: invalidate, onError,
  });
  const revoke = useMutation({
    mutationFn: (accountId: string) => api.revokeAccess(user.id, accountId),
    onSuccess: invalidate, onError,
  });

  const owned = accounts.filter((a) => a.owner_id === user.id);
  const ownedIds = new Set(owned.map((a) => a.id));
  const byId = new Map(accounts.map((a) => [a.id, a]));
  const granted = user.account_ids
    .filter((id) => !ownedIds.has(id))
    .map((id) => byId.get(id))
    .filter((a): a is Account => Boolean(a));
  const grantable = accounts.filter(
    (a) => !ownedIds.has(a.id) && !user.account_ids.includes(a.id),
  );
  // null means an admin has granted nothing, which is 0 - the backend reads
  // it the same way (workspace_limit in dashboard_api/main.py).
  const limit = user.max_workspaces ?? 0;
  const quotaPct = limit === 0
    ? 0
    : Math.min(100, Math.round((user.owned_workspaces / limit) * 100));
  const quotaFull = user.owned_workspaces >= limit;

  return (
    <Modal onClose={onClose} title={`Chi tiết ${user.email}`}>
      <div className="product-form-header">
        <h3>{user.email}</h3>
        <button onClick={onClose} aria-label="Đóng">×</button>
      </div>

      <div className="user-card-pill-row" style={{ marginBottom: 4 }}>
        <StatusPill active={user.is_active} pending={user.is_pending} />
        <RolePill admin={user.is_admin} />
      </div>
      <p className="user-card-sub" style={{ marginBottom: 16 }}>
        Tạo ngày {formatDate(user.created_at)}
      </p>

      <div className="user-detail-stats">
        <div><span className="detail-section-label">Token Gemini</span><div className="n">{formatTokens(user.usage.total_tokens)}</div></div>
        <div><span className="detail-section-label">Prompt</span><div className="n">{formatTokens(user.usage.prompt_tokens)}</div></div>
        <div><span className="detail-section-label">Phản hồi</span><div className="n">{formatTokens(user.usage.completion_tokens)}</div></div>
        <div><span className="detail-section-label">Lượt gọi</span><div className="n">{formatTokens(user.usage.calls)}</div></div>
      </div>

      {error && <div className="banner bad">{error}</div>}

      {!user.is_admin && (
        <div className="user-quota" style={{ margin: "16px 0" }}>
          <div className="stat-card-bar">
            <div
              className="stat-card-bar-fill"
              style={{
                width: `${quotaPct}%`,
                background: quotaFull ? "var(--red)" : "var(--primary)",
              }}
            />
          </div>
          <div className="user-quota-row">
            <span className="detail-section-label">Giới hạn workspace</span>
            <span className={`user-quota-value${quotaFull ? " full" : ""}`}>
              {user.owned_workspaces} / {limit}
            </span>
          </div>
        </div>
      )}

      <div className="user-workspaces">
        <div className="user-section-title">
          <FolderIcon width={12} height={12} /> Sở hữu <span className="user-count">{owned.length}</span>
        </div>
        {owned.length === 0 ? (
          <p className="user-workspaces-empty">Chưa tạo workspace nào.</p>
        ) : (
          <ul className="user-chip-list">
            {owned.map((a) => (
              <li key={a.id} className="user-chip owned" title={`Chủ sở hữu: ${user.email}`}>
                {a.label}
              </li>
            ))}
          </ul>
        )}

        {user.is_admin ? (
          <p className="user-workspaces-note">Admin thấy và thao tác được mọi workspace.</p>
        ) : (
          <>
            <div className="user-section-title">Được cấp quyền <span className="user-count">{granted.length}</span></div>
            <ul className="user-chip-list">
              {granted.map((a) => (
                <li key={a.id} className="user-chip granted">
                  {a.label}
                  <button
                    type="button"
                    className="user-chip-remove"
                    title={`Thu hồi quyền vào ${a.label}`}
                    aria-label={`Thu hồi quyền vào ${a.label}`}
                    disabled={revoke.isPending}
                    onClick={() => revoke.mutate(a.id)}
                  >
                    ×
                  </button>
                </li>
              ))}
              {grantable.length > 0 && (
                <li className="user-chip add">
                  <select
                    value=""
                    disabled={grant.isPending}
                    aria-label="Cấp quyền vào workspace"
                    onChange={(event) => { if (event.target.value) grant.mutate(event.target.value); }}
                  >
                    <option value="">+ Cấp quyền…</option>
                    {grantable.map((a) => (
                      <option key={a.id} value={a.id}>{a.label}</option>
                    ))}
                  </select>
                </li>
              )}
              {granted.length === 0 && grantable.length === 0 && (
                <li className="user-workspaces-empty">Không còn workspace nào để cấp.</li>
              )}
            </ul>
          </>
        )}
      </div>
    </Modal>
  );
}

// -------------------------------------------------------------- Chỉnh sửa
function UserEditModal({
  user, isSelf, onClose,
}: {
  user: ManagedUser;
  isSelf: boolean;
  onClose: () => void;
}) {
  const client = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [passwordDraft, setPasswordDraft] = useState("");
  // Empty input = 0, not "unlimited". There is deliberately no way to type
  // "unlimited": every workspace costs a Chrome and an agent process on the
  // server, so the cap is a real budget an admin has to name.
  const [limitDraft, setLimitDraft] = useState(String(user.max_workspaces ?? 0));

  const invalidate = () => client.invalidateQueries({ queryKey: ["users"] });
  const onError = (err: Error) => { setNotice(null); setError(err.message); };

  const sendResetLink = useMutation({
    mutationFn: () => api.sendUserResetLink(user.id),
    onSuccess: () => {
      setError(null);
      setNotice(`Đã gửi mật khẩu tạm thời tới ${user.email} (hiệu lực 30 phút). Mật khẩu hiện tại vẫn dùng được cho tới khi mật khẩu tạm được dùng.`);
    },
    onError,
  });
  const setPassword = useMutation({
    mutationFn: () => api.setUserPassword(user.id, passwordDraft),
    onSuccess: () => { setPasswordDraft(""); setError(null); setNotice("Đã đổi mật khẩu."); },
    onError,
  });
  const setLimit = useMutation({
    mutationFn: () => {
      const trimmed = limitDraft.trim();
      return api.setUserMaxWorkspaces(user.id, trimmed === "" ? 0 : Number(trimmed));
    },
    onSuccess: () => { invalidate(); setError(null); setNotice("Đã lưu giới hạn workspace."); },
    onError,
  });
  const deactivate = useMutation({ mutationFn: () => api.deactivateUser(user.id), onSuccess: invalidate, onError });
  const activate = useMutation({ mutationFn: () => api.activateUser(user.id), onSuccess: invalidate, onError });
  const remove = useMutation({
    mutationFn: () => api.deleteUser(user.id),
    onSuccess: () => { invalidate(); onClose(); },
    onError,
  });

  const busy = sendResetLink.isPending || setPassword.isPending || setLimit.isPending ||
    deactivate.isPending || activate.isPending || remove.isPending;
  const limitInvalid = limitDraft.trim() !== "" &&
    (!Number.isInteger(Number(limitDraft.trim())) || Number(limitDraft.trim()) < 0);

  return (
    <Modal onClose={onClose} title={`Chỉnh sửa ${user.email}`}>
      <div className="product-form-header">
        <h3>Chỉnh sửa · {user.email}</h3>
        <button onClick={onClose} aria-label="Đóng">×</button>
      </div>

      {error && <div className="banner bad">{error}</div>}
      {notice && <div className="banner info">{notice}</div>}

      <section className="user-edit-section">
        <div className="user-section-title">Mật khẩu</div>
        <div className="user-inline-form">
          <input
            type="password"
            value={passwordDraft}
            onChange={(event) => setPasswordDraft(event.target.value)}
            placeholder="Mật khẩu mới (tối thiểu 8 ký tự)"
          />
          <button className="primary" disabled={passwordDraft.length < 8 || busy} onClick={() => setPassword.mutate()}>
            Lưu mật khẩu
          </button>
          <button disabled={busy} onClick={() => sendResetLink.mutate()}>Gửi mật khẩu tạm qua email</button>
        </div>
      </section>

      {!user.is_admin && (
        <section className="user-edit-section">
          <div className="user-section-title">Giới hạn workspace</div>
          <div className="user-inline-form">
            <NumberInput
              aria-label="Số workspace được tạo"
              value={limitDraft}
              onValueChange={setLimitDraft}
              placeholder="0"
            />
            <button className="primary" disabled={limitInvalid || busy} onClick={() => setLimit.mutate()}>Lưu</button>
            {/* A div, not a span: it carries a <ul>, which is not valid
                inside a span. Three separate rules were one run-on line and
                the one that surprises people - granted workspaces are not
                counted - was buried at the end of it. */}
            <div className="user-inline-hint">
              Đang sở hữu <strong>{user.owned_workspaces}</strong> workspace.
              <ul className="user-hint-list">
                <li><strong>0</strong> = không cho tự tạo workspace nào.</li>
                <li>
                  Hạ giới hạn xuống thấp hơn số đang có chỉ chặn tạo mới,{" "}
                  <strong>không xoá gì</strong>.
                </li>
                <li>Workspace được admin cấp quyền <strong>không</strong> tính vào giới hạn này.</li>
              </ul>
            </div>
          </div>
        </section>
      )}

      {!isSelf && (
        <section className="user-edit-section user-edit-danger">
          <div className="user-section-title">Trạng thái tài khoản</div>
          <div className="user-inline-form">
            {user.is_active ? (
              <button disabled={busy} onClick={() => deactivate.mutate()}>Vô hiệu hoá</button>
            ) : (
              <button disabled={busy} onClick={() => activate.mutate()}>
                <CheckCircleIcon width={13} height={13} style={{ marginRight: 4, verticalAlign: -2 }} />
                {/* "lại" only when there is something to go back to. A
                    pending signup has never been active, so offering to
                    reactivate it describes a history it does not have -
                    and hides the one action that actually needs doing. */}
                {user.is_pending ? "Kích hoạt" : "Kích hoạt lại"}
              </button>
            )}
            <button
              className="danger"
              disabled={busy}
              onClick={() => {
                if (window.confirm(`Xoá tài khoản "${user.email}"? Không thể hoàn tác.`)) remove.mutate();
              }}
            >
              Xoá tài khoản
            </button>
          </div>
        </section>
      )}
    </Modal>
  );
}

// ----------------------------------------------------------------- trang
type StatusFilter = "all" | "active" | "pending" | "inactive";

export function UsersAdmin({
  accounts, currentUserId,
}: {
  accounts: Account[];
  currentUserId: number;
}) {
  const client = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [detailId, setDetailId] = useState<number | null>(null);
  const [editId, setEditId] = useState<number | null>(null);

  // The "+ Thêm người dùng" inline form.
  const [creating, setCreating] = useState(false);
  const [newEmail, setNewEmail] = useState("");
  const newEmailIssue = gmailProblem(newEmail);
  const [newPassword, setNewPassword] = useState("");

  const users = useQuery({ queryKey: ["users"], queryFn: api.users });
  const invalidate = () => client.invalidateQueries({ queryKey: ["users"] });
  const onError = (err: Error) => { setNotice(null); setError(err.message); };

  const createUser = useMutation({
    mutationFn: () => api.createUser(newEmail.trim().toLowerCase(), newPassword),
    onSuccess: () => {
      setCreating(false); setNewEmail(""); setNewPassword(""); setError(null);
      setNotice("Đã tạo tài khoản. Gửi mật khẩu cho người dùng qua kênh riêng của bạn.");
      invalidate();
    },
    onError,
  });

  // This page manages *other* operators - there is nothing here an admin
  // does to their own account (no self-deactivate/delete, see
  // UserEditModal's `isSelf` gate below), and a password change for
  // yourself does not belong buried in a list of other people. Excluded
  // everywhere below (stats, tabs, grid) so the numbers shown always match
  // what is actually on screen.
  const allUsers = (users.data ?? []).filter((u) => u.id !== currentUserId);
  const activeCount = allUsers.filter((u) => u.is_active).length;
  const pendingCount = allUsers.filter((u) => u.is_pending).length;
  const inactiveCount = allUsers.length - activeCount - pendingCount;

  const visibleUsers = (
    statusFilter === "active" ? allUsers.filter((u) => u.is_active) :
    statusFilter === "pending" ? allUsers.filter((u) => u.is_pending) :
    statusFilter === "inactive" ? allUsers.filter((u) => !u.is_active && !u.is_pending) :
    allUsers
  );

  // Re-derived from the live list every render (not the object captured when
  // the dialog opened), so a grant/revoke/edit inside it shows up without
  // closing and reopening - and the dialog quietly disappears if the user
  // it was showing gets deleted from underneath it.
  const detailUser = detailId == null ? null : allUsers.find((u) => u.id === detailId) ?? null;
  const editUser = editId == null ? null : allUsers.find((u) => u.id === editId) ?? null;

  return (
    <div className="main">
      <div className="dashboard-head">
        <div className="users-admin-title">
          <span className="stat-card-icon brand users-admin-title-icon">
            <UsersIcon width={20} height={20} />
          </span>
          <div>
            <h2>Quản lý người dùng</h2>
            <p className="sub" style={{ marginBottom: 0 }}>
              Ai đăng nhập được vào dashboard này, workspace họ sở hữu hay được cấp
              quyền, giới hạn workspace, và lượng token Gemini đã dùng.
            </p>
          </div>
        </div>
        <div className="users-admin-actions">
          <button onClick={() => downloadUsersCsv(allUsers)} disabled={allUsers.length === 0}>
            Xuất dữ liệu
          </button>
        </div>
      </div>

      {creating && (
        <form
          className="panel users-admin-create-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (newEmail.trim() && !newEmailIssue && newPassword.length >= 8) createUser.mutate();
          }}
        >
          <input
            type="email"
            autoComplete="off"
            aria-label="Gmail của tài khoản mới"
            placeholder="tenban@gmail.com"
            value={newEmail}
            aria-invalid={Boolean(newEmailIssue)}
            onChange={(event) => setNewEmail(event.target.value)}
            autoFocus
          />
          <input
            type="password"
            placeholder="Mật khẩu (tối thiểu 8 ký tự)"
            value={newPassword}
            onChange={(event) => setNewPassword(event.target.value)}
          />
          <button
            className="primary"
            type="submit"
            disabled={
              !newEmail.trim() || Boolean(newEmailIssue) || newPassword.length < 8 || createUser.isPending
            }
          >
            Tạo tài khoản
          </button>
          {newEmailIssue && (
            <small className="product-form-error" role="alert">{newEmailIssue}</small>
          )}
          <button type="button" onClick={() => setCreating(false)}>Huỷ</button>
          <span className="user-inline-hint">
            Luôn tạo tài khoản thường (chưa có workspace nào) - tài khoản admin chỉ tạo
            được bằng lệnh <code>python -m dashboard_api.create_admin</code> ở backend.
          </span>
        </form>
      )}

      {/* User management is account + permission data, not a photo gallery
          - a table reads it in one pass (scan a column, compare rows)
          the way the card grid never could once there were more than a
          handful of people. */}
      <section className="panel users-table-panel">
        <div className="users-summary">
          <span><strong>{allUsers.length}</strong> người dùng</span>
          <span><strong>{activeCount}</strong> đang hoạt động</span>
          {pendingCount > 0 && (
            <span className="bad"><strong>{pendingCount}</strong> chờ duyệt</span>
          )}
          <span><strong>{accounts.length}</strong> workspace</span>
        </div>

        <div className="filter-tabs">
          {([
            ["all", `Tất cả (${allUsers.length})`],
            ["active", `Đang hoạt động (${activeCount})`],
            ["pending", `Chờ duyệt (${pendingCount})`],
            ["inactive", `Đã vô hiệu hoá (${inactiveCount})`],
          ] as const).map(([value, label]) => (
            <button
              key={value}
              className={`filter-tab${statusFilter === value ? " active" : ""}`}
              onClick={() => setStatusFilter(value)}
            >
              {label}
            </button>
          ))}
        </div>

        {error && <div className="banner bad">{error}</div>}
        {notice && <div className="banner info">{notice}</div>}
        {users.isLoading && <p className="empty">Đang tải...</p>}
        {!users.isLoading && visibleUsers.length === 0 && (
          <p className="empty">
            {allUsers.length === 0 ? "Chưa có tài khoản nào." : "Không có tài khoản nào ở trạng thái này."}
          </p>
        )}

        {visibleUsers.length > 0 && (
          <div className="admin-table-wrap">
            <table className="admin-table">
              <thead>
                <tr>
                  <th>Người dùng</th>
                  <th>Vai trò</th>
                  <th>Workspace</th>
                  <th>Trạng thái</th>
                  <th className="sr-only">Hành động</th>
                </tr>
              </thead>
              <tbody>
                {visibleUsers.map((user) => {
                  const owned = accounts.filter((a) => a.owner_id === user.id).length;
                  return (
                    <tr key={user.id}>
                      <td>
                        <span className={`admin-table-dot${user.is_active ? " ok" : " bad"}`} aria-hidden />
                        {user.email}
                      </td>
                      <td>{user.is_admin ? "Admin" : "Member"}</td>
                      {/* Owned vs. the quota: the pair is what an admin
                          scans the table for ("who is out of room?"), and
                          either number alone does not answer it. */}
                      <td>{user.is_admin ? owned : `${owned} / ${user.max_workspaces ?? 0}`}</td>
                      <td><StatusPill active={user.is_active} pending={user.is_pending} /></td>
                      <td className="admin-table-actions">
                        <button onClick={() => setDetailId(user.id)}>Chi tiết</button>
                        <button onClick={() => setEditId(user.id)}>Chỉnh sửa</button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        <div className="users-table-footer">
          <button className="primary" onClick={() => setCreating((v) => !v)}>
            <PlusIcon width={15} height={15} style={{ marginRight: 6, verticalAlign: -3 }} />
            Thêm người dùng
          </button>
        </div>
      </section>

      {detailUser && (
        <UserDetailModal user={detailUser} accounts={accounts} onClose={() => setDetailId(null)} />
      )}
      {editUser && (
        <UserEditModal user={editUser} isSelf={editUser.id === currentUserId} onClose={() => setEditId(null)} />
      )}
    </div>
  );
}

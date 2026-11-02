import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { Account, AgentEvent } from "../lib/api";
import { api } from "../lib/api";
import { actionLabel, eventDescription, eventLabel } from "../lib/labels";
import { ActivityIcon, CheckCircleIcon, LayersIcon, XCircleIcon } from "../components/ui/icons";

export function ActivityPage({ account, events }: { account: Account; events: AgentEvent[] }) {
  const [showRaw, setShowRaw] = useState(false);

  const activity = useQuery({
    queryKey: ["activity", account.id],
    queryFn: () => api.activity(account.id, 80),
    refetchInterval: 5000,
  });

  const liveEvents = events.filter((event) => event.account_id === account.id).slice(0, 40);
  const totalActions = activity.data?.length ?? 0;
  const successCount = activity.data?.filter((row) => row.success === 1).length ?? 0;
  const failCount = activity.data?.filter((row) => row.success === 0).length ?? 0;

  return (
    <main className="main">
      <header style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
        <h2>Hoạt động</h2>
        <button onClick={() => setShowRaw((value) => !value)}>
          {showRaw ? "Ẩn raw logs" : "Xem raw logs"}
        </button>
      </header>
      <p className="sub">Nhật ký hoạt động của {account.label}, viết lại dễ đọc từ log kỹ thuật.</p>

      <div className="stat-cards" style={{ marginBottom: 20 }}>
        <div className="stat-card tint-brand">
          <LayersIcon className="stat-card-decor" width={64} height={64} />
          <div className="stat-card-top">
            <span className="stat-card-eyebrow">Sự kiện gần đây</span>
            <span className="stat-card-icon brand"><LayersIcon width={18} height={18} /></span>
          </div>
          <div className="n">{liveEvents.length}</div>
          <div className="t">Đang hiển thị bên dưới</div>
        </div>
        <div className="stat-card tint-info">
          <ActivityIcon className="stat-card-decor" width={64} height={64} />
          <div className="stat-card-top">
            <span className="stat-card-eyebrow">Hành động đã ghi</span>
            <span className="stat-card-icon info"><ActivityIcon width={18} height={18} /></span>
          </div>
          <div className="n">{totalActions}</div>
          <div className="t">Của {account.label}</div>
        </div>
        <div className="stat-card tint-success">
          <CheckCircleIcon className="stat-card-decor" width={64} height={64} />
          <div className="stat-card-top">
            <span className="stat-card-eyebrow">Thành công</span>
            <span className="stat-card-icon success"><CheckCircleIcon width={18} height={18} /></span>
          </div>
          <div className="n">{successCount}</div>
          <div className="t">Hành động chạy đúng</div>
        </div>
        <div className="stat-card tint-danger">
          <XCircleIcon className="stat-card-decor" width={64} height={64} />
          <div className="stat-card-top">
            <span className="stat-card-eyebrow">Lỗi</span>
            <span className="stat-card-icon danger"><XCircleIcon width={18} height={18} /></span>
          </div>
          <div className="n">{failCount}</div>
          <div className="t">Hành động thất bại</div>
        </div>
      </div>

      <div className="grid">
        <section className="panel">
          <h3>Sự kiện hệ thống</h3>
          <div className="activity-list">
            {liveEvents.length === 0 && <p className="empty">Chưa có sự kiện nào</p>}
            {liveEvents.map((event, index) => (
              <div className="activity-item" key={`${event.ts}-${index}`}>
                <span className="activity-item-time">
                  {new Date(event.ts).toLocaleTimeString("vi-VN")}
                </span>
                <span className="activity-item-body">
                  <strong>{eventLabel(event)}</strong>
                  {eventDescription(event) && (
                    <span className="activity-item-desc"> — {eventDescription(event)}</span>
                  )}
                  {showRaw && (
                    <div className="activity-item-raw">
                      <code>{event.type}</code> {JSON.stringify(event.payload)}
                    </div>
                  )}
                </span>
              </div>
            ))}
          </div>
        </section>

        <section className="panel">
          <h3>Hành động của Agent</h3>
          <div className="activity-list">
            {(activity.data?.length ?? 0) === 0 && <p className="empty">Chưa có hành động nào được ghi</p>}
            {activity.data?.map((row) => (
              <div className="activity-item" key={row.id}>
                <span className="activity-item-time">
                  {new Date(row.created_at).toLocaleTimeString("vi-VN")}
                </span>
                <span className="activity-item-body">
                  <strong>{actionLabel(row)}</strong>
                  {row.error && <span className="activity-item-desc text-bad"> — {row.error}</span>}
                  {showRaw && (
                    <div className="activity-item-raw">
                      <code>{row.action_name}</code>
                      {row.capability && <> · {row.capability}</>}
                      · task <code>{row.task_id}</code>
                    </div>
                  )}
                </span>
                <span className={`tag ${row.success === 1 ? "ok" : row.success === 0 ? "bad" : ""}`}>
                  {row.success === 1 ? "ok" : row.success === 0 ? "fail" : "—"}
                </span>
              </div>
            ))}
          </div>
        </section>
      </div>
    </main>
  );
}

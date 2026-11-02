// Admin-only "Log" page: the raw process logs of any workspace, read from
// data/accounts/<id>/logs/*.log through GET /api/logs/{id}. Unlike Activity
// (a friendly rewrite of one workspace's events for the people running it),
// this is the technical log itself, for whoever is diagnosing the agent - so
// every workspace on the host is selectable, not only the admin's own.
import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import type { Account, LogFileName, LogLevel, WorkspaceLog } from "../lib/api";

const FILE_LABEL: Record<LogFileName, string> = {
  runtime: "Agent (runtime.log)",
  chrome: "Chrome",
  xvfb: "Xvfb",
  x11vnc: "x11vnc",
  novnc: "noVNC",
};

// Minimum level shown. WARNING includes ERROR/CRITICAL, as `logging` does.
const LEVEL_FILTERS: { value: LogLevel | null; label: string }[] = [
  { value: null, label: "Tất cả" },
  { value: "WARNING", label: "Cảnh báo trở lên" },
  { value: "ERROR", label: "Chỉ lỗi" },
];

const LINE_OPTIONS = [200, 500, 1000, 2000];

function formatBytes(n: number): string {
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  if (n >= 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${n} B`;
}

function downloadLog(accountId: string, log: WorkspaceLog): void {
  const text = log.entries.map((entry) => entry.text).join("\n");
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `${accountId}-${log.file}-${new Date().toISOString().slice(0, 19).replace(/:/g, "")}.log`;
  link.click();
  URL.revokeObjectURL(url);
}

export function LogsPage({
  accounts, initialAccountId,
}: { accounts: Account[]; initialAccountId: string | null }) {
  const [accountId, setAccountId] = useState<string | null>(
    initialAccountId && accounts.some((a) => a.id === initialAccountId)
      ? initialAccountId
      : accounts[0]?.id ?? null,
  );
  const [file, setFile] = useState<LogFileName>("runtime");
  const [level, setLevel] = useState<LogLevel | null>(null);
  const [lines, setLines] = useState(500);
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [follow, setFollow] = useState(true);
  const viewport = useRef<HTMLPreElement>(null);
  // Only pin to the bottom while the reader is already there: scrolling up
  // to look at an earlier traceback must not get yanked away by a refresh.
  const atBottom = useRef(true);

  useEffect(() => {
    if (!accountId && accounts.length) setAccountId(accounts[0].id);
  }, [accounts, accountId]);

  // Typing filters server-side; wait for a pause instead of a request a key.
  useEffect(() => {
    const timer = setTimeout(() => setQuery(search.trim()), 350);
    return () => clearTimeout(timer);
  }, [search]);

  const log = useQuery({
    queryKey: ["workspace-log", accountId, file, level, lines, query],
    queryFn: () => api.workspaceLog(accountId!, { file, lines, level, q: query }),
    enabled: accountId !== null,
    refetchInterval: follow ? 3000 : false,
    placeholderData: (previous) => previous,
  });

  useEffect(() => {
    const node = viewport.current;
    if (node && atBottom.current) node.scrollTop = node.scrollHeight;
  }, [log.data]);

  // A new selection starts at the newest line again.
  useEffect(() => {
    atBottom.current = true;
  }, [accountId, file, level, lines, query]);

  const account = accounts.find((a) => a.id === accountId);
  const data = log.data;
  const available = new Set(data?.files.map((f) => f.name) ?? []);

  return (
    <main className="main logs-page">
      <header>
        <h2>Nhật ký hệ thống</h2>
      </header>
      <p className="sub">
        Log kỹ thuật của agent và các tiến trình đi kèm, đọc từ phần cuối file. Chỉ admin xem được.
      </p>

      {accounts.length === 0 ? (
        <p className="empty">Chưa có workspace nào.</p>
      ) : (
        <>
          <div className="toolbar logs-toolbar">
            <label className="logs-field">
              <span>Workspace</span>
              <select value={accountId ?? ""} onChange={(e) => setAccountId(e.target.value)}>
                {accounts.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.label} ({a.id}){a.processes.agent.running ? "" : " · đã dừng"}
                  </option>
                ))}
              </select>
            </label>
            <label className="logs-field">
              <span>File</span>
              <select value={file} onChange={(e) => setFile(e.target.value as LogFileName)}>
                {(Object.keys(FILE_LABEL) as LogFileName[]).map((name) => (
                  <option key={name} value={name} disabled={data !== undefined && !available.has(name)}>
                    {FILE_LABEL[name]}
                  </option>
                ))}
              </select>
            </label>
            <label className="logs-field">
              <span>Số dòng</span>
              <select value={lines} onChange={(e) => setLines(Number(e.target.value))}>
                {LINE_OPTIONS.map((n) => <option key={n} value={n}>{n} mục cuối</option>)}
              </select>
            </label>
            <label className="logs-field logs-search">
              <span>Tìm</span>
              <input
                type="search"
                value={search}
                placeholder="tên khách, task_id, ERROR…"
                onChange={(e) => setSearch(e.target.value)}
              />
            </label>
          </div>

          <div className="logs-controls">
            <div className="filter-tabs">
              {LEVEL_FILTERS.map((option) => (
                <button
                  key={option.label}
                  type="button"
                  className={`filter-tab${level === option.value ? " active" : ""}`}
                  onClick={() => setLevel(option.value)}
                >
                  {option.label}
                </button>
              ))}
            </div>
            <div className="logs-actions">
              <label className="logs-follow">
                <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} />
                Tự làm mới (3 giây)
              </label>
              <button type="button" onClick={() => log.refetch()} disabled={log.isFetching}>
                Làm mới
              </button>
              <button
                type="button"
                onClick={() => data && accountId && downloadLog(accountId, data)}
                disabled={!data || data.entries.length === 0}
              >
                Tải xuống
              </button>
            </div>
          </div>

          {log.isError && <div className="banner bad">{(log.error as Error).message}</div>}

          <div className="logs-meta">
            {account && <strong>{account.label}</strong>}
            {data && (
              <>
                <span>{FILE_LABEL[data.file]}</span>
                <span>{formatBytes(data.size)}</span>
                <span>{data.entries.length} mục</span>
                {data.truncated && (
                  <span className="logs-truncated">
                    Chỉ quét phần cuối file, có thể còn kết quả cũ hơn
                  </span>
                )}
              </>
            )}
          </div>

          <pre
            ref={viewport}
            className="logs-viewport"
            onScroll={(e) => {
              const node = e.currentTarget;
              atBottom.current = node.scrollHeight - node.scrollTop - node.clientHeight < 40;
            }}
          >
            {log.isLoading ? (
              <span className="logs-empty">Đang tải…</span>
            ) : !data || data.entries.length === 0 ? (
              <span className="logs-empty">
                {data && !available.has(file)
                  ? "Workspace này chưa có file log này."
                  : "Không có dòng nào khớp bộ lọc."}
              </span>
            ) : (
              data.entries.map((entry, index) => (
                <div key={index} className={`log-entry level-${(entry.level ?? "none").toLowerCase()}`}>
                  {entry.text}
                </div>
              ))
            )}
          </pre>
        </>
      )}
    </main>
  );
}

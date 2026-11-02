import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../lib/api";
import type { Account, SearchResults } from "../lib/api";
import type { Page } from "../App";
import { Modal } from "./Modal";
import { ORDER_STATUS_LABEL } from "../lib/labels";
import { formatMoney } from "../lib/format";

type Props = {
  open: boolean;
  onClose: () => void;
  accountId: string | null;
  /** Workspaces this user can see - searchable, and selecting one switches. */
  accounts: Account[];
  onNavigate: (page: Page) => void;
  onSelectWorkspace: (accountId: string) => void;
  onOpenConversation: (conversationId: string) => void;
  onOpenOrder: (orderId: number) => void;
  onOpenProduct: (productId: string) => void;
};

const PAGE_COMMANDS: { page: Page; label: string; hint: string }[] = [
  { page: "home", label: "Mở Tổng quan", hint: "Workspace & vận hành" },
  { page: "inbox", label: "Mở Tin nhắn", hint: "Hội thoại khách hàng" },
  { page: "orders", label: "Mở Đơn hàng", hint: "Danh sách đơn đã ghi" },
  { page: "products", label: "Mở Sản phẩm", hint: "Danh mục sản phẩm" },
  { page: "automation", label: "Mở Tự động hóa", hint: "Cấu hình 24/7" },
  { page: "scenario", label: "Mở Kịch bản", hint: "Ngữ cảnh & cách tiếp cận khách hàng" },
  { page: "activity", label: "Mở Hoạt động", hint: "Nhật ký hoạt động" },
  { page: "alerts", label: "Mở Cảnh báo", hint: "Cảnh báo cần chú ý" },
];

// Same folding as dashboard_api/search.py so local (pages, workspaces) and
// server (customers, orders, products) matching agree: "don hang" finds
// "Đơn hàng".
function fold(value: string | null | undefined): string {
  return (value ?? "")
    .replace(/đ/g, "d").replace(/Đ/g, "D")
    .normalize("NFD").replace(/[̀-ͯ]/g, "")
    .toLowerCase();
}

function matches(term: string, ...fields: (string | null | undefined)[]): boolean {
  const words = fold(term).split(/\s+/).filter(Boolean);
  if (words.length === 0) return false;
  const haystack = fields.map(fold).join(" ");
  return words.every((word) => haystack.includes(word));
}

// One flat, ordered list of everything selectable, so ↑/↓/Enter walk the
// groups in the order they render without each group keeping its own index.
type Item = { key: string; group: string; label: string; hint: string; run: () => void };

const EMPTY: SearchResults = { customers: [], orders: [], products: [] };
const DEBOUNCE_MS = 180;
const MIN_TERM = 2;

export function CommandPalette({
  open, onClose, accountId, accounts,
  onNavigate, onSelectWorkspace, onOpenConversation, onOpenOrder, onOpenProduct,
}: Props) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResults>(EMPTY);
  const [searching, setSearching] = useState(false);
  const [cursor, setCursor] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (open) { setQuery(""); setResults(EMPTY); setCursor(0); }
  }, [open]);

  const term = query.trim();

  // Server search: debounced, and the previous in-flight request is aborted
  // so a fast typist never sees results for an older prefix land after the
  // newer ones. Under MIN_TERM characters nothing is sent - "a" would match
  // half the inbox and tell the operator nothing.
  useEffect(() => {
    if (!open || !accountId || term.length < MIN_TERM) {
      setResults(EMPTY);
      setSearching(false);
      return;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      setSearching(true);
      try {
        setResults(await api.search(accountId, term, controller.signal));
      } catch (error) {
        if ((error as Error).name !== "AbortError") setResults(EMPTY);
      } finally {
        if (!controller.signal.aborted) setSearching(false);
      }
    }, DEBOUNCE_MS);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [open, accountId, term]);

  const items = useMemo<Item[]>(() => {
    const out: Item[] = [];
    const finish = (fn: () => void) => () => { fn(); onClose(); };

    for (const c of results.customers) {
      const name = c.display_name || c.username || c.customer_id || c.conversation_id;
      out.push({
        key: `c:${c.conversation_id}`, group: "Khách hàng", label: name,
        hint: c.matched === "message" ? (c.snippet ?? "") : [c.username && `@${c.username}`, c.phone].filter(Boolean).join(" · ") || "Tin nhắn",
        run: finish(() => onOpenConversation(c.conversation_id)),
      });
    }
    for (const o of results.orders) {
      out.push({
        key: `o:${o.id}`, group: "Đơn hàng", label: `#${o.id} · ${o.customer_name}`,
        hint: [(ORDER_STATUS_LABEL as Record<string, string>)[o.status] ?? o.status, formatMoney(o.total_amount), o.items].filter(Boolean).join(" · "),
        run: finish(() => onOpenOrder(o.id)),
      });
    }
    for (const p of results.products) {
      out.push({
        key: `p:${p.id}`, group: "Sản phẩm", label: p.name,
        hint: [formatMoney(p.price), p.stock != null && `tồn ${p.stock}`, !p.is_active && "ngừng bán"].filter(Boolean).join(" · "),
        run: finish(() => onOpenProduct(p.id)),
      });
    }
    if (term) {
      for (const a of accounts) {
        if (a.id === accountId || !matches(term, a.label, a.id)) continue;
        out.push({
          key: `w:${a.id}`, group: "Workspace", label: a.label, hint: "Chuyển workspace",
          run: finish(() => onSelectWorkspace(a.id)),
        });
      }
    }
    for (const cmd of PAGE_COMMANDS) {
      if (term && !matches(term, cmd.label, cmd.hint)) continue;
      out.push({
        key: `n:${cmd.page}`, group: "Điều hướng", label: cmd.label, hint: cmd.hint,
        run: finish(() => onNavigate(cmd.page)),
      });
    }
    return out;
  }, [results, accounts, accountId, term, onClose, onNavigate, onSelectWorkspace, onOpenConversation, onOpenOrder, onOpenProduct]);

  // Results changed under the cursor: clamp rather than reset, so arrowing
  // while results stream in doesn't keep snapping back to the top.
  useEffect(() => {
    setCursor((c) => Math.min(c, Math.max(0, items.length - 1)));
  }, [items.length]);

  // Keep the highlighted row in view when moving with the keyboard.
  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>(`[data-index="${cursor}"]`);
    // Optional-call: jsdom (tests) has no scrollIntoView, and nothing here
    // depends on it beyond keeping the row visible.
    el?.scrollIntoView?.({ block: "nearest" });
  }, [cursor]);

  if (!open) return null;

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setCursor((c) => (items.length ? (c + 1) % items.length : 0));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setCursor((c) => (items.length ? (c - 1 + items.length) % items.length : 0));
    } else if (event.key === "Enter") {
      event.preventDefault();
      items[cursor]?.run();
    }
  };

  // Group headers are emitted where the group changes in the flat list.
  let lastGroup = "";
  const noWorkspace = !accountId;
  const nothing = term.length >= MIN_TERM && !searching && items.length === 0;

  return (
    <Modal onClose={onClose} title="Tìm kiếm và điều hướng" className="command-palette" overlayClassName="command-palette-overlay">
      <input
        autoFocus
        className="command-palette-input"
        placeholder={noWorkspace ? "Điều hướng..." : "Tìm khách hàng, tin nhắn, đơn hàng, sản phẩm, workspace..."}
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        onKeyDown={onKeyDown}
        aria-label="Tìm kiếm"
        aria-activedescendant={items[cursor] ? `palette-${items[cursor].key}` : undefined}
      />
      <div className="command-palette-results" ref={listRef} role="listbox">
        {searching && <p className="command-palette-status">Đang tìm…</p>}
        {noWorkspace && term.length >= MIN_TERM && (
          <p className="command-palette-status">Chọn một workspace để tìm khách hàng, đơn hàng, sản phẩm.</p>
        )}
        {nothing && <p className="command-palette-empty">Không tìm thấy gì cho "{term}"</p>}
        {items.map((item, index) => {
          const header = item.group !== lastGroup ? item.group : null;
          lastGroup = item.group;
          return (
            <div key={item.key}>
              {header && <div className="command-palette-group-label">{header}</div>}
              <button
                id={`palette-${item.key}`}
                data-index={index}
                role="option"
                aria-selected={index === cursor}
                className={`command-palette-item${index === cursor ? " active" : ""}`}
                onMouseEnter={() => setCursor(index)}
                onClick={item.run}
              >
                <span className="command-palette-item-label">{item.label}</span>
                <span className="command-palette-item-hint">{item.hint}</span>
              </button>
            </div>
          );
        })}
      </div>
      <div className="command-palette-footer">
        <span><kbd>↑</kbd><kbd>↓</kbd> di chuyển</span>
        <span><kbd>Enter</kbd> mở</span>
        <span><kbd>Esc</kbd> đóng</span>
      </div>
    </Modal>
  );
}

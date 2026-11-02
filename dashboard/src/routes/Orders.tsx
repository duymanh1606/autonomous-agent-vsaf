import { useEffect, useId, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Conversation, Order, OrderInput, OrderStatus } from "../lib/api";
import { api } from "../lib/api";
import {
  ORDER_STATUS_ACTION_LABEL,
  ORDER_STATUS_LABEL,
  ORDER_STATUS_TINT,
  ORDER_STATUS_TRANSITIONS,
  isOrderLocked,
} from "../lib/labels";
import { formatMoney } from "../lib/format";
import {
  HOLDS_STOCK, addProduct, computeTotal, missingOrderFields, orderPayload, overStock, rowsFromOrder,
  type OrderLineRow,
} from "../lib/orderForm";
import { LayersIcon, PackageIcon, ReceiptIcon, TrashIcon } from "../components/ui/icons";
import { useToast } from "../components/Toast";
import { Modal } from "../components/Modal";
import { NumberInput } from "../components/NumberInput";
import { ProductPicker } from "../components/ProductPicker";

function currency(value: number | null): string {
  return formatMoney(value, { whenNull: "chưa có" });
}

function shortDate(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString("vi-VN", { day: "2-digit", month: "2-digit", year: "numeric" });
}

const EMPTY_FORM: OrderInput = {
  customer_name: "", conversation_id: null, total_amount: null, status: "pending",
  shipping_address: "", note: "",
};

function orderToForm(order: Order): OrderInput {
  return {
    customer_name: order.customer_name,
    // Sent back on every edit: the server would otherwise have nothing to
    // keep, and an unlinked order has no row on the Google Sheet.
    conversation_id: order.conversation_id,
    total_amount: order.total_amount,
    status: order.status,
    shipping_address: order.shipping_address,
    note: order.note,
  };
}

function OrderFormModal({
  accountId, editing, onClose,
}: {
  accountId: string;
  editing: Order | null;
  onClose: () => void;
}) {
  const client = useQueryClient();
  const [form, setForm] = useState<OrderInput>(editing ? orderToForm(editing) : EMPTY_FORM);
  const [rows, setRows] = useState<OrderLineRow[]>(() => rowsFromOrder(editing));
  // An order logged before the product picker has only this typed text. It
  // is kept as-is until products are picked to replace it.
  const legacyItems = editing && !editing.line_items?.length ? editing.items : "";
  // The total follows the picked products until the operator types their own
  // (a discount, a shipping fee) - from then on it is theirs. An existing
  // order whose total already differs from its products starts that way.
  const [totalTouched, setTotalTouched] = useState(
    () => Boolean(editing) && editing!.total_amount !== computeTotal(rowsFromOrder(editing)),
  );
  const [error, setError] = useState<string | null>(null);
  const productsLabel = useId();
  const totalId = useId();
  const catalogue = useQuery({
    queryKey: ["products", accountId],
    queryFn: () => api.products(accountId),
  });
  // Same list (and cache) as the Inbox page.
  const conversations = useQuery({
    queryKey: ["conversations", accountId],
    queryFn: () => api.conversations(accountId),
  });
  const conversationName = (item: Conversation) =>
    item.display_name || item.username || item.conversation_id;

  const changeRows = (next: OrderLineRow[]) => {
    setRows(next);
    if (!totalTouched) setForm((prev) => ({ ...prev, total_amount: computeTotal(next) }));
  };
  const computed = computeTotal(rows);
  const payload = orderPayload(form, rows, legacyItems);
  const stockById = Object.fromEntries(
    (catalogue.data?.products ?? []).map((product) => [product.id, product.stock ?? null]),
  );
  const originalQty = Object.fromEntries(
    (editing?.line_items ?? []).map((line) => [line.product_id, line.quantity]),
  );
  // Only judged once the catalogue has loaded; until then nothing is known.
  // What this order took out of stock when it was placed, still held.
  const held = editing?.stock_deducted && HOLDS_STOCK.has(editing.status) ? originalQty : {};
  const short = catalogue.data ? overStock(rows, stockById, originalQty, held) : {};

  // An order moves stock, so the catalogue is refetched along with the orders.
  const invalidate = () => {
    client.invalidateQueries({ queryKey: ["orders", accountId] });
    client.invalidateQueries({ queryKey: ["products", accountId] });
  };

  const create = useMutation({
    mutationFn: () => api.createOrder(accountId, payload),
    onSuccess: () => { invalidate(); onClose(); },
    onError: (err: Error) => setError(err.message),
  });

  const update = useMutation({
    mutationFn: () => api.updateOrder(accountId, editing!.id, payload),
    onSuccess: () => { invalidate(); onClose(); },
    onError: (err: Error) => setError(err.message),
  });

  const saving = create.isPending || update.isPending;
  const missing = [
    ...missingOrderFields(payload),
    ...(Object.keys(short).length ? ["Số lượng không vượt tồn kho"] : []),
  ];
  const canSubmit = missing.length === 0;

  return (
    <Modal onClose={onClose} title={editing ? "Sửa đơn hàng" : "Ghi đơn hàng mới"}>
        <div className="product-form-header">
          <h3>{editing ? "Sửa đơn hàng" : "Ghi đơn hàng mới"}</h3>
          <button onClick={onClose}>Đóng</button>
        </div>

        {error && <div className="banner bad">{error}</div>}

        <div className="product-form-grid">
          <label className="product-form-field">
            <span className="is-required">Tên khách hàng</span>
            <input aria-required="true"
              value={form.customer_name}
              onChange={(event) => setForm((prev) => ({ ...prev, customer_name: event.target.value }))}
              placeholder="vd: Nguyễn Văn A"
              autoFocus
            />
          </label>
          <label className="product-form-field">
            <span>Hội thoại TikTok</span>
            <select
              value={form.conversation_id ?? ""}
              onChange={(event) => {
                const picked = conversations.data?.find(
                  (item) => item.conversation_id === event.target.value,
                );
                setForm((prev) => ({
                  ...prev,
                  conversation_id: picked?.conversation_id ?? null,
                  customer_name: prev.customer_name || (picked ? conversationName(picked) : ""),
                }));
              }}
            >
              <option value="">— Không gắn (không lên Google Sheet) —</option>
              {/* An order linked to a conversation no longer in the list
                  keeps its link rather than losing it on save. */}
              {form.conversation_id &&
                !conversations.data?.some((item) => item.conversation_id === form.conversation_id) && (
                  <option value={form.conversation_id}>{form.conversation_id}</option>
                )}
              {(conversations.data ?? []).map((item) => (
                <option key={item.conversation_id} value={item.conversation_id}>
                  {conversationName(item)}
                  {item.username && item.display_name ? ` (@${item.username})` : ""}
                </option>
              ))}
            </select>
          </label>
          <div className="product-form-field">
            {/* A div, not a wrapping <label>: the hint and its button below
                must not become part of the field's name. */}
            <label htmlFor={totalId} className="is-required">Tổng tiền (đ)</label>
            <NumberInput aria-required="true"
              id={totalId}
              value={form.total_amount == null ? "" : String(form.total_amount)}
              onValueChange={(value) => {
                setTotalTouched(true);
                setForm((prev) => ({ ...prev, total_amount: value ? Number(value) : null }));
              }}
            />
            {rows.length > 0 && !totalTouched && (
              <span className="hint">Tự tính từ sản phẩm. Sửa được nếu có giảm giá hoặc phí ship.</span>
            )}
            {rows.length > 0 && totalTouched && computed != null && computed !== form.total_amount && (
              <span className="hint">
                Theo sản phẩm: {formatMoney(computed)}{" "}
                <button
                  type="button"
                  className="link-button"
                  onClick={() => {
                    setTotalTouched(false);
                    setForm((prev) => ({ ...prev, total_amount: computed }));
                  }}
                >
                  Dùng số này
                </button>
              </span>
            )}
          </div>
          {editing && (
            <label className="product-form-field">
              <span>Trạng thái</span>
              <select
                value={form.status}
                disabled={isOrderLocked(editing.status)}
                onChange={(event) => setForm((prev) => ({ ...prev, status: event.target.value as OrderStatus }))}
              >
                <option value={editing.status}>{ORDER_STATUS_LABEL[editing.status]}</option>
                {ORDER_STATUS_TRANSITIONS[editing.status].map((status) => (
                  <option key={status} value={status}>{ORDER_STATUS_ACTION_LABEL[status]}</option>
                ))}
              </select>
              {isOrderLocked(editing.status) && (
                <span className="hint">Đơn đã bàn giao đơn vị vận chuyển - trạng thái bị khoá.</span>
              )}
            </label>
          )}
        </div>

        <div className="product-form-field product-form-field-wide">
          <span className="is-required" id={productsLabel}>Sản phẩm</span>
          {legacyItems && (
            <span className="hint">
              Ghi tay trước đây: {legacyItems}
              {rows.length ? " (sẽ được thay bằng các sản phẩm đã chọn)" : ""}
            </span>
          )}
          <ProductPicker
            products={catalogue.data?.products ?? []}
            loading={catalogue.isLoading}
            labelledBy={productsLabel}
            onPick={(product) => changeRows(addProduct(rows, product))}
          />
          {catalogue.isError && (
            <span className="hint form-missing">Không tải được danh sách sản phẩm. Đóng form và thử lại.</span>
          )}
          {rows.length > 0 && (
            <ul className="order-lines" aria-label="Sản phẩm trong đơn">
              {rows.map((row, index) => (
                <li key={row.product_id} className={row.product_id in short ? "is-short" : undefined}>
                  <span className="order-line-name">{row.name}</span>
                  <span className="order-line-price">
                    {formatMoney(row.unit_price, { whenNull: "chưa có giá" })}
                  </span>
                  <NumberInput
                    className="order-line-qty"
                    aria-label={`Số lượng ${row.name}`}
                    aria-invalid={row.product_id in short || undefined}
                    value={row.quantity}
                    onValueChange={(quantity) =>
                      changeRows(rows.map((item, at) => (at === index ? { ...item, quantity } : item)))
                    }
                  />
                  <span className="order-line-total">
                    {row.unit_price != null && Number(row.quantity) > 0
                      ? formatMoney(row.unit_price * Number(row.quantity))
                      : ""}
                  </span>
                  <button
                    type="button"
                    className="order-line-remove"
                    aria-label={`Bỏ ${row.name}`}
                    onClick={() => changeRows(rows.filter((_, at) => at !== index))}
                  >
                    ×
                  </button>
                  {row.product_id in short && (
                    <span className="order-line-error" role="alert">
                      Chỉ còn {short[row.product_id]} trong kho.
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
        <label className="product-form-field product-form-field-wide">
          <span className="is-required">Địa chỉ giao hàng</span>
          <textarea aria-required="true"
            rows={2}
            value={form.shipping_address}
            onChange={(event) => setForm((prev) => ({ ...prev, shipping_address: event.target.value }))}
          />
        </label>
        <label className="product-form-field product-form-field-wide">
          <span>Ghi chú (không bắt buộc)</span>
          <textarea
            rows={2}
            value={form.note}
            onChange={(event) => setForm((prev) => ({ ...prev, note: event.target.value }))}
          />
        </label>

        <div className="product-form-footer">
          {!canSubmit && (
            <span className="hint form-missing" role="status">
              Còn thiếu: {missing.join(", ")}
            </span>
          )}
          <button onClick={onClose}>Huỷ</button>
          <button
            className="primary"
            disabled={!canSubmit || saving}
            onClick={() => (editing ? update.mutate() : create.mutate())}
          >
            {editing ? "Lưu thay đổi" : "Tạo đơn hàng"}
          </button>
        </div>
    </Modal>
  );
}

export function Orders({
  accountId, focusId, onFocused,
}: {
  accountId: string;
  // Set by the ⌘K palette to open one order's form straight away - applied
  // once the list has loaded, then cleared via onFocused so it doesn't
  // re-fire on the next refetch (same shape as Customers.focusConversationId).
  focusId?: number | null;
  onFocused?: () => void;
}) {
  const client = useQueryClient();
  const toast = useToast();
  const [formOpen, setFormOpen] = useState<"create" | Order | null>(null);
  const [error, setError] = useState<string | null>(null);

  const orders = useQuery({
    queryKey: ["orders", accountId],
    queryFn: () => api.orders(accountId),
    refetchInterval: 15000,
  });
  // Deployment-wide, not per workspace; only changes with a supervisor restart.
  const sheet = useQuery({ queryKey: ["sheet-link"], queryFn: api.sheetLink, staleTime: 5 * 60_000 });
  const sheetUrl = sheet.data?.url ?? "";

  const remove = useMutation({
    mutationFn: (orderId: number) => api.deleteOrder(accountId, orderId),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["orders", accountId] });
      client.invalidateQueries({ queryKey: ["products", accountId] });
    },
    onError: (err: Error) => setError(err.message),
  });

  const setStatus = useMutation({
    mutationFn: ({ orderId, status }: { orderId: number; status: OrderStatus }) =>
      api.setOrderStatus(accountId, orderId, status),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["orders", accountId] });
      client.invalidateQueries({ queryKey: ["products", accountId] });
    },
    onError: (err: Error) => toast.error("Không đổi được trạng thái đơn", err.message),
  });

  const list = orders.data ?? [];

  useEffect(() => {
    if (focusId == null) return;
    const target = orders.data?.find((o) => o.id === focusId);
    if (target) {
      setFormOpen(target);
      onFocused?.();
      return;
    }
    // Not in the (possibly stale) cached list yet - force a fresh fetch
    // before giving up, so a just-created/updated order found via ⌘K
    // search still opens instead of being silently dropped.
    let cancelled = false;
    orders.refetch().then((result) => {
      if (cancelled) return;
      const fresh = result.data?.find((o) => o.id === focusId);
      if (fresh) setFormOpen(fresh);
      onFocused?.();
    });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusId]);
  const pendingCount = list.filter((o) => o.status === "pending" || o.status === "packing").length;
  const shippingCount = list.filter((o) => o.status === "shipping").length;
  const revenue = list.filter((o) => o.status === "done").reduce((sum, o) => sum + (o.total_amount ?? 0), 0);

  return (
    <div className="main">
      <header style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
        <h2>Đơn hàng</h2>
        <div style={{ display: "flex", gap: 8 }}>
          <button
            disabled={!sheetUrl}
            onClick={() => window.open(sheetUrl, "_blank", "noopener,noreferrer")}
            title={
              sheetUrl
                ? "Mở Google Sheet khách hàng"
                : "Chưa cấu hình Google Sheet (GSHEET_URL trong .env)"
            }
          >
            Xuất Google Sheet
          </button>
          <button className="primary" onClick={() => setFormOpen("create")}>
            + Ghi đơn mới
          </button>
        </div>
      </header>
      <p className="sub">
        Đơn hàng do bạn tự ghi lại khi chốt với khách trong hội thoại - hệ thống chưa đọc
        đơn hàng thật từ TikTok Shop.
      </p>

      {error && <div className="banner bad">{error}</div>}

      {list.length > 0 && (
        <div className="stat-cards" style={{ marginBottom: 20 }}>
          <div className="stat-card tint-brand">
            <PackageIcon className="stat-card-decor" width={64} height={64} />
            <div className="stat-card-top">
              <span className="stat-card-eyebrow">Tổng đơn</span>
              <span className="stat-card-icon brand"><PackageIcon width={18} height={18} /></span>
            </div>
            <div className="n">{list.length}</div>
            <div className="t">Trên workspace này</div>
          </div>
          <div className="stat-card tint-warn">
            <ReceiptIcon className="stat-card-decor" width={64} height={64} />
            <div className="stat-card-top">
              <span className="stat-card-eyebrow">Chờ xử lý</span>
              <span className="stat-card-icon warn"><ReceiptIcon width={18} height={18} /></span>
            </div>
            <div className="n">{pendingCount}</div>
            <div className="t">Chờ xác nhận / đóng gói</div>
          </div>
          <div className="stat-card tint-info">
            <LayersIcon className="stat-card-decor" width={64} height={64} />
            <div className="stat-card-top">
              <span className="stat-card-eyebrow">Đang giao</span>
              <span className="stat-card-icon info"><LayersIcon width={18} height={18} /></span>
            </div>
            <div className="n">{shippingCount}</div>
            <div className="t">Đơn đang trên đường</div>
          </div>
          <div className="stat-card tint-success">
            <ReceiptIcon className="stat-card-decor" width={64} height={64} />
            <div className="stat-card-top">
              <span className="stat-card-eyebrow">Doanh thu</span>
              <span className="stat-card-icon success"><ReceiptIcon width={18} height={18} /></span>
            </div>
            <div className="n" style={{ fontSize: 22 }}>{currency(revenue)}</div>
            <div className="t">Từ đơn đã hoàn tất</div>
          </div>
        </div>
      )}

      {list.length === 0 ? (
        <p className="empty">Chưa có đơn hàng nào - bấm "Ghi đơn mới" khi chốt đơn với khách.</p>
      ) : (
        <div className="order-list">
          {list.map((order) => (
            <div className="order-card" key={order.id}>
              <div className="order-card-main">
                <div className="order-card-title">
                  {order.customer_name}
                  <span className={`tag ${ORDER_STATUS_TINT[order.status]}`}>
                    {ORDER_STATUS_LABEL[order.status]}
                  </span>
                </div>
                {order.items && <div className="order-card-detail">{order.items}</div>}
                <div className="order-card-meta">
                  {currency(order.total_amount)} · {shortDate(order.created_at)}
                  {order.shipping_address && ` · ${order.shipping_address}`}
                </div>
                {isOrderLocked(order.status) && (
                  <div className="order-card-meta">🔒 Đã bàn giao đơn vị vận chuyển - trạng thái bị khoá</div>
                )}
              </div>
              <div className="order-card-actions">
                {ORDER_STATUS_TRANSITIONS[order.status].map((next) => (
                  <button
                    key={next}
                    disabled={setStatus.isPending}
                    onClick={() => {
                      if (
                        next !== "cancelled" ||
                        window.confirm(`Huỷ đơn hàng của "${order.customer_name}"?`)
                      ) {
                        setStatus.mutate({ orderId: order.id, status: next });
                      }
                    }}
                  >
                    {ORDER_STATUS_ACTION_LABEL[next]}
                  </button>
                ))}
                <button onClick={() => setFormOpen(order)}>Sửa</button>
                <button
                  className="danger"
                  disabled={remove.isPending}
                  onClick={() => {
                    if (window.confirm(`Xoá đơn hàng của "${order.customer_name}"?`)) {
                      remove.mutate(order.id);
                    }
                  }}
                >
                  <TrashIcon width={14} height={14} />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {formOpen && (
        <OrderFormModal
          accountId={accountId}
          editing={formOpen === "create" ? null : formOpen}
          onClose={() => setFormOpen(null)}
        />
      )}
    </div>
  );
}

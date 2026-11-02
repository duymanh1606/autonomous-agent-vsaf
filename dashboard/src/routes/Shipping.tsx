import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ShippingSettings } from "../lib/api";
import { api } from "../lib/api";

// The shop's delivery facts (products.shipping). The agent may quote exactly
// these - and totals built from them - and nothing else about delivery; a
// blank field is never stated to a customer.
type Draft = {
  fee: string;
  free_from_quantity: string;
  delivery_days: string;
  area: string;
  cash_on_delivery: boolean;
};

function toDraft(shipping: ShippingSettings | null | undefined): Draft {
  return {
    fee: shipping?.fee?.toString() ?? "",
    free_from_quantity: shipping?.free_from_quantity?.toString() ?? "",
    delivery_days: shipping?.delivery_days ?? "",
    area: shipping?.area ?? "",
    cash_on_delivery: shipping?.cash_on_delivery ?? false,
  };
}

function fromDraft(draft: Draft): ShippingSettings | null {
  const result: ShippingSettings = {};
  if (draft.fee.trim()) result.fee = Number(draft.fee);
  if (draft.free_from_quantity.trim()) result.free_from_quantity = Number(draft.free_from_quantity);
  if (draft.delivery_days.trim()) result.delivery_days = draft.delivery_days.trim();
  if (draft.area.trim()) result.area = draft.area.trim();
  if (draft.cash_on_delivery) result.cash_on_delivery = true;
  return Object.keys(result).length ? result : null;
}

export function Shipping({ accountId }: { accountId: string }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<Draft>(toDraft(null));
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const shipping = useQuery({
    queryKey: ["shipping", accountId],
    queryFn: () => api.shipping(accountId),
  });

  useEffect(() => {
    if (shipping.data !== undefined) setDraft(toDraft(shipping.data.shipping));
  }, [shipping.data, accountId]);

  const save = useMutation({
    mutationFn: (value: ShippingSettings | null) => api.saveShipping(accountId, value),
    onSuccess: (data) => {
      queryClient.setQueryData(["shipping", accountId], data);
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
    },
    onError: (err: Error) => setError(err.message),
  });

  const update = (field: keyof Draft, value: string | boolean) => {
    setDraft((previous) => ({ ...previous, [field]: value }));
    setError(null);
  };

  const handleSave = () => {
    const value = fromDraft(draft);
    if (value?.fee !== undefined && (!Number.isFinite(value.fee) || value.fee < 0)) {
      setError("Phí ship phải là một số không âm.");
      return;
    }
    if (
      value?.free_from_quantity !== undefined
      && (!Number.isInteger(value.free_from_quantity) || value.free_from_quantity < 1)
    ) {
      setError("Số lượng miễn ship phải là số nguyên từ 1.");
      return;
    }
    save.mutate(value);
  };

  const field = { display: "grid", gap: 4, fontSize: 13 } as const;
  const input = { padding: 8, fontSize: 14, maxWidth: 260 } as const;

  return (
    <section className="panel" style={{ marginBottom: 14 }}>
      <h3 style={{ margin: 0 }}>Giao hàng</h3>
      <div style={{ color: "var(--muted)", fontSize: 13, margin: "6px 0 10px" }}>
        Agent chỉ được nói về phí ship, thời gian giao và thanh toán khi nhận hàng theo đúng
        các ô dưới đây (kể cả tổng tiền = số lượng × giá + phí ship). Ô để trống thì agent
        không nói tới.
      </div>
      {(error || shipping.isError) && (
        <div className="banner bad">{error ?? "Không tải được thông tin giao hàng."}</div>
      )}
      <div style={{ display: "grid", gap: 10, gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))" }}>
        <label style={field}>
          Phí ship (số)
          <input
            type="number" min={0} inputMode="numeric" style={input}
            value={draft.fee} onChange={(event) => update("fee", event.target.value)}
            placeholder="6000"
          />
        </label>
        <label style={field}>
          Miễn ship từ (số lượng)
          <input
            type="number" min={1} step={1} inputMode="numeric" style={input}
            value={draft.free_from_quantity}
            onChange={(event) => update("free_from_quantity", event.target.value)}
            placeholder="2"
          />
        </label>
        <label style={field}>
          Thời gian giao
          <input
            style={input} value={draft.delivery_days} maxLength={120}
            onChange={(event) => update("delivery_days", event.target.value)}
            placeholder="2-5 ngày"
          />
        </label>
        <label style={field}>
          Khu vực giao
          <input
            style={input} value={draft.area} maxLength={120}
            onChange={(event) => update("area", event.target.value)}
            placeholder="Toàn Myanmar"
          />
        </label>
      </div>
      <label style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 10, fontSize: 14 }}>
        <input
          type="checkbox" checked={draft.cash_on_delivery}
          onChange={(event) => update("cash_on_delivery", event.target.checked)}
        />
        Nhận hàng mới trả tiền (COD)
      </label>
      <div style={{ display: "flex", gap: 10, alignItems: "center", marginTop: 10 }}>
        <button
          className="primary"
          disabled={shipping.isLoading || save.isPending}
          onClick={handleSave}
        >
          Lưu giao hàng
        </button>
        {saved && <span style={{ color: "var(--ok)", fontSize: 12 }}>Đã lưu</span>}
      </div>
    </section>
  );
}

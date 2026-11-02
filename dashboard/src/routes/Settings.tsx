import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { FollowUpRule, SettingsResult } from "../lib/api";
import { api } from "../lib/api";
import { NumberInput } from "../components/NumberInput";

const FOLLOWUP_MAX_RULES = 20;
const FOLLOWUP_MAX_MESSAGE_CHARACTERS = 1000;
const FOLLOWUP_MAX_DELAY_MINUTES = 60 * 24 * 14; // 14 ngày, khớp agent/followup_policy.py

type DelayUnit = "phút" | "giờ" | "ngày";
const UNIT_MINUTES: Record<DelayUnit, number> = { "phút": 1, "giờ": 60, "ngày": 60 * 24 };

function minutesToDisplay(minutes: number): { value: string; unit: DelayUnit } {
  if (minutes > 0 && minutes % UNIT_MINUTES["ngày"] === 0) {
    return { value: String(minutes / UNIT_MINUTES["ngày"]), unit: "ngày" };
  }
  if (minutes > 0 && minutes % UNIT_MINUTES["giờ"] === 0) {
    return { value: String(minutes / UNIT_MINUTES["giờ"]), unit: "giờ" };
  }
  return { value: String(minutes), unit: "phút" };
}

type DraftRule = {
  id?: string;
  value: string;
  unit: DelayUnit;
  message: string;
  enabled: boolean;
};

function ruleToDraft(rule: FollowUpRule): DraftRule {
  const { value, unit } = minutesToDisplay(rule.delay_minutes);
  return { id: rule.id, value, unit, message: rule.message, enabled: rule.enabled };
}

const EMPTY_DRAFT: DraftRule = { value: "1", unit: "giờ", message: "", enabled: true };

// Only keys `control/settings.py` can actually apply to a running agent. The
// page deliberately does not offer keys that silently need a restart, except
// where it can say so - a settings screen that appears to work but does not is
// worse than one that refuses.
// Ranges come from the server (GET .../settings), which is also what
// enforces them - the quiet period's upper bound depends on the workspace's
// own reply_wait_timeout_seconds.
const FIELDS: {
  key: string;
  label: string;
  hint: string;
  restartRequired?: boolean;
}[] = [
  {
    key: "messaging.auto_check.interval_seconds",
    label: "Bao lâu quét inbox một lần (giây)",
    hint: "Mỗi bao nhiêu giây agent quét lại inbox tìm tin mới. Cần khởi động lại workspace mới có hiệu lực.",
    restartRequired: true,
  },
  {
    key: "messaging.auto_check.reply_quiet_period_seconds",
    label: "Chờ tin nhắn trong bao lâu (giây)",
    hint: "Tin chưa trả lời phải đứng yên bấy nhiêu giây thì agent mới trả lời - tránh trả lời khi khách còn đang gõ.",
  },
];

function fieldError(raw: string, limit?: { min: number; max: number }): string | null {
  if (raw.trim() === "") return "Nhập một số.";
  const value = Number(raw);
  if (!Number.isFinite(value)) return "Phải là một số.";
  if (limit && (value < limit.min || value > limit.max)) {
    return `Trong khoảng ${limit.min} - ${limit.max} giây.`;
  }
  return null;
}

const FIELD_LABEL: Record<string, string> = Object.fromEntries(
  FIELDS.map((field) => [field.key, field.label]),
);

function settingLabel(key: string): string {
  return FIELD_LABEL[key] ?? key.split(".").pop() ?? key;
}

function sameRules(a: FollowUpRule[], b: FollowUpRule[]): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

function FollowUpEditor({ accountId }: { accountId: string }) {
  const [drafts, setDrafts] = useState<DraftRule[]>([]);
  const [dirty, setDirty] = useState(false);
  const [saved, setSaved] = useState(false);
  const [validationError, setValidationError] = useState<string | null>(null);
  const queryClient = useQueryClient();
  const queryKey = ["followups", accountId];

  const followups = useQuery({
    queryKey,
    queryFn: () => api.followups(accountId),
  });

  // Only fill the cards from the server the first time they load (or when
  // switching account) - same reasoning as the Scenario page: once the
  // operator has started editing, a later change to the cached rules (a
  // refetch, or a delete writing its result back below) must never clobber
  // unsaved cards underneath them.
  const loadedForRef = useRef<string | null>(null);
  useEffect(() => {
    if (followups.data === undefined || loadedForRef.current === accountId) return;
    loadedForRef.current = accountId;
    setDrafts(
      followups.data.rules.length
        ? followups.data.rules.map(ruleToDraft)
        : [{ ...EMPTY_DRAFT }],
    );
    setDirty(false);
  }, [followups.data, accountId]);

  // Every write replaces the whole list on the server, so the cache has to
  // follow each one. It used to keep the list from page load: a second delete
  // was then computed from that stale list and brought back the rule the
  // first delete had removed.
  const save = useMutation({
    mutationFn: (rules: (Omit<FollowUpRule, "id"> & { id?: string })[]) =>
      api.saveFollowups(accountId, rules),
    onSuccess: (data) => queryClient.setQueryData(queryKey, data),
  });

  const savedRules = followups.data?.rules ?? [];

  const updateCard = <K extends keyof DraftRule>(index: number, field: K, value: DraftRule[K]) => {
    setDrafts((previous) =>
      previous.map((card, i) => (i === index ? { ...card, [field]: value } : card)),
    );
    setDirty(true);
    setValidationError(null);
  };

  const dropCard = (predicate: (card: DraftRule, i: number) => boolean) =>
    setDrafts((previous) => {
      const next = previous.filter((card, i) => !predicate(card, i));
      return next.length ? next : [{ ...EMPTY_DRAFT }];
    });

  const removeCard = (index: number) => {
    const card = drafts[index];
    const hasContent = Boolean(card.id || card.message.trim());
    if (hasContent && !window.confirm(`Xoá luật ${index + 1}? Không thể hoàn tác.`)) return;
    setValidationError(null);
    if (!card.id) {
      // Never saved: nothing on the server to remove.
      dropCard((_, i) => i === index);
      setDirty(true);
      return;
    }
    // A rule already saved on the server has to disappear for real, not just
    // from this draft - otherwise "Xoá" without a separate "Lưu" looks like
    // it worked and the rule comes back on the next reload. Only this rule is
    // taken out of what the server holds, so unsaved edits on the other cards
    // stay on screen as unsaved instead of being written or thrown away. The
    // card goes once the server has agreed, so a failed delete leaves it
    // visible rather than hiding a rule that is still active.
    save.mutate(savedRules.filter((rule) => rule.id !== card.id), {
      onSuccess: () => dropCard((draft) => draft.id === card.id),
    });
  };

  const addCard = () => setDrafts((previous) => [...previous, { ...EMPTY_DRAFT }]);

  const handleSave = () => {
    const cleaned: (Omit<FollowUpRule, "id"> & { id?: string })[] = [];
    for (const card of drafts) {
      const message = card.message.trim();
      const rawValue = card.value.trim();
      if (!message && !rawValue) continue; // an untouched blank card, silently dropped
      if (!message || !rawValue) {
        setValidationError(
          "Mỗi luật cần cả thời gian chờ và tin nhắn sẽ gửi - hoặc để trống cả hai.",
        );
        return;
      }
      const numericValue = Number(rawValue);
      if (!Number.isFinite(numericValue) || numericValue <= 0) {
        setValidationError("Thời gian chờ phải là một số dương.");
        return;
      }
      const delayMinutes = Math.round(numericValue * UNIT_MINUTES[card.unit]);
      if (delayMinutes > FOLLOWUP_MAX_DELAY_MINUTES) {
        setValidationError("Thời gian chờ tối đa là 14 ngày.");
        return;
      }
      if (message.length > FOLLOWUP_MAX_MESSAGE_CHARACTERS) {
        setValidationError(`Tin nhắn tối đa ${FOLLOWUP_MAX_MESSAGE_CHARACTERS} ký tự.`);
        return;
      }
      cleaned.push({
        ...(card.id ? { id: card.id } : {}),
        delay_minutes: delayMinutes, message, enabled: card.enabled,
      });
    }
    if (cleaned.length > FOLLOWUP_MAX_RULES) {
      setValidationError(`Tối đa ${FOLLOWUP_MAX_RULES} luật.`);
      return;
    }
    setValidationError(null);
    save.mutate(cleaned, {
      onSuccess: (data) => {
        setDrafts(data.rules.length ? data.rules.map(ruleToDraft) : [{ ...EMPTY_DRAFT }]);
        setDirty(false);
        setSaved(true);
        setTimeout(() => setSaved(false), 3000);
      },
    });
  };

  const cleanedDrafts: FollowUpRule[] = drafts
    .filter((card) => card.message.trim() || card.value.trim())
    .map((card) => ({
      id: card.id ?? "",
      delay_minutes: Math.round((Number(card.value) || 0) * UNIT_MINUTES[card.unit]),
      message: card.message.trim(),
      enabled: card.enabled,
    }));
  const hasUnsavedChanges = dirty && !sameRules(
    cleanedDrafts.map(({ id, ...rest }) => rest as FollowUpRule),
    savedRules.map(({ id, ...rest }) => rest as FollowUpRule),
  );

  return (
    <section className="panel">
      <h3>Tự động nhắc khách im lặng</h3>
      <p style={{ color: "var(--muted)", marginTop: 0 }}>
        Mỗi luật là: khách im lặng bao lâu kể từ tin cuối cùng của shop thì tự động
        gửi tin nhắn gì. Chỉ nhắc trong hội thoại đã có khách nhắn trước đó, không
        bao giờ nhắn cho người lạ, và mỗi luật chỉ gửi đúng 1 lần cho mỗi lần khách
        im lặng - khách nhắn lại thì lần im lặng kế tiếp mới được nhắc tiếp. Bị tạm
        dừng khi hội thoại đang có người tiếp quản hoặc đang chờ người tiếp quản, và
        chỉ chạy trong giờ hoạt động (mục bên dưới). Lưu được ngay cả khi agent chưa
        chạy; nếu agent đang chạy thì áp dụng luôn, không cần khởi động lại.
      </p>

      {followups.isError && (
        <div className="banner bad">Không tải được danh sách luật hiện tại.</div>
      )}
      {(validationError || save.isError) && (
        <div className="banner bad">
          {validationError
            ?? (save.error instanceof Error ? save.error.message : "Lưu thất bại.")}
        </div>
      )}

      <div className="grid" style={{ gap: 14 }}>
        {drafts.map((card, index) => (
          <div className="panel" key={card.id ?? index} style={{ background: "var(--panel-light)" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
              <h4 style={{ margin: 0 }}>Luật {index + 1}</h4>
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <label style={{ fontSize: 13, display: "flex", gap: 4, alignItems: "center" }}>
                  <input
                    type="checkbox"
                    checked={card.enabled}
                    onChange={(event) => updateCard(index, "enabled", event.target.checked)}
                  />
                  Bật
                </label>
                <button
                  type="button"
                  className="danger"
                  // One write at a time: each sends the whole list, so two in
                  // flight would each be built without the other's change.
                  disabled={save.isPending}
                  onClick={() => removeCard(index)}
                  aria-label={`Xoá luật ${index + 1}`}
                >
                  Xoá
                </button>
              </div>
            </div>

            <label style={{ display: "block", margin: "10px 0 4px", fontSize: 13, color: "var(--muted)" }}>
              Khách im lặng
            </label>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <NumberInput
                decimal
                aria-label={`Thời gian chờ luật ${index + 1}`}
                disabled={followups.isLoading}
                value={card.value}
                onValueChange={(value) => updateCard(index, "value", value)}
                style={{ width: 90 }}
              />
              <select
                disabled={followups.isLoading}
                value={card.unit}
                onChange={(event) => updateCard(index, "unit", event.target.value as DelayUnit)}
              >
                <option value="phút">phút</option>
                <option value="giờ">giờ</option>
                <option value="ngày">ngày</option>
              </select>
              <span style={{ fontSize: 13, color: "var(--muted)" }}>thì gửi:</span>
            </div>

            <textarea
              value={card.message}
              disabled={followups.isLoading}
              onChange={(event) => updateCard(index, "message", event.target.value)}
              placeholder="Ví dụ: Dạ shop nhắn hỏi thêm, bạn còn cần shop hỗ trợ gì nữa không ạ?"
              rows={2}
              style={{
                width: "100%", maxWidth: 640, resize: "vertical", fontFamily: "inherit",
                fontSize: 14, padding: 10, boxSizing: "border-box", marginTop: 8,
              }}
            />
            <div style={{ color: "var(--muted)", fontSize: 12, marginTop: 4 }}>
              {card.message.length}/{FOLLOWUP_MAX_MESSAGE_CHARACTERS} ký tự
            </div>
          </div>
        ))}
      </div>

      <div style={{ display: "flex", gap: 10, alignItems: "center", marginTop: 14 }}>
        <button type="button" disabled={drafts.length >= FOLLOWUP_MAX_RULES} onClick={addCard}>
          + Thêm luật
        </button>
        <button
          className="primary"
          disabled={followups.isLoading || save.isPending || !hasUnsavedChanges}
          onClick={handleSave}
        >
          Lưu
        </button>
        {saved && !hasUnsavedChanges && (
          <span style={{ color: "var(--ok)", fontSize: 12 }}>Đã lưu</span>
        )}
      </div>
    </section>
  );
}

export function Settings({ accountId, running }: { accountId: string; running: boolean }) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [result, setResult] = useState<SettingsResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const current = useQuery({
    queryKey: ["pacing-settings", accountId],
    queryFn: () => api.readSettings(accountId),
  });

  // Fill the inputs with what the workspace is set to now, so "giữ nguyên"
  // is no longer a guess. Only when the saved values change (first load, a
  // save, another workspace) - never over what the operator is typing.
  useEffect(() => {
    if (!current.data) return;
    setValues(Object.fromEntries(
      FIELDS.map((field) => {
        const value = current.data.values[field.key];
        return [field.key, value == null ? "" : String(value)];
      }),
    ));
  }, [current.data]);

  const saved = (key: string) => {
    const value = current.data?.values[key];
    return value == null ? "" : String(value);
  };
  const changedKeys = FIELDS.map((field) => field.key).filter(
    (key) => (values[key] ?? "") !== saved(key),
  );
  const errors = Object.fromEntries(
    changedKeys
      .map((key) => [key, fieldError(values[key] ?? "", current.data?.limits[key])] as const)
      .filter(([, message]) => message),
  ) as Record<string, string>;

  const apply = useMutation({
    mutationFn: () => {
      const payload: Record<string, unknown> = {};
      for (const key of changedKeys) payload[key] = Number(values[key]);
      return api.settings(accountId, payload);
    },
    onSuccess: (data) => {
      setResult(data);
      setError(null);
      current.refetch();
    },
    onError: (err: Error) => {
      setError(err.message);
      setResult(null);
    },
  });

  const hasResultRows = Boolean(
    result &&
      (Object.keys(result.applied).length ||
        Object.keys(result.restart_required).length ||
        Object.keys(result.rejected).length),
  );

  return (
    <div className="grid">
      <section className="panel">
        <div
          style={{
            display: "flex", alignItems: "center", justifyContent: "space-between",
            margin: "0 0 14px", paddingBottom: 12, borderBottom: "1px solid #edf0f4",
          }}
        >
          <h3 style={{ margin: 0, padding: 0, border: "none" }}>Nhịp quét &amp; trả lời</h3>
          <span className={`tag ${running ? "ok" : "warn"}`}>
            {running ? "Agent đang chạy" : "Agent chưa chạy"}
          </span>
        </div>
        {error && <div className="banner bad">{error}</div>}
        {current.isError && (
          <div className="banner bad">Không tải được giá trị hiện tại.</div>
        )}
        {!running && (
          <div className="banner">
            Agent chưa chạy: thay đổi sẽ được lưu và có hiệu lực khi workspace chạy.
          </div>
        )}

        <div
          style={{
            display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
            gap: "14px 24px",
          }}
        >
          {FIELDS.map((field) => (
            <div key={field.key}>
              <label style={{ display: "block", marginBottom: 4 }}>
                {field.label}
                {field.restartRequired && (
                  <span style={{ color: "var(--warn)", fontSize: 11, marginLeft: 6 }}>
                    CẦN KHỞI ĐỘNG LẠI
                  </span>
                )}
              </label>
              <NumberInput
                decimal
                aria-label={field.label}
                disabled={!current.data}
                aria-invalid={Boolean(errors[field.key])}
                value={values[field.key] ?? ""}
                onValueChange={(value) =>
                  setValues((previous) => ({ ...previous, [field.key]: value }))
                }
                style={{ width: "100%", maxWidth: 200, boxSizing: "border-box" }}
              />
              {errors[field.key] && (
                <small className="product-form-error" role="alert">{errors[field.key]}</small>
              )}
              <div style={{ color: "var(--muted)", fontSize: 12, marginTop: 4 }}>
                {field.hint}
                {current.data?.limits[field.key] && (
                  <> Cho phép {current.data.limits[field.key].min} - {current.data.limits[field.key].max} giây.</>
                )}
              </div>
            </div>
          ))}
        </div>

        <div
          style={{
            display: "flex", gap: 10, alignItems: "center",
            marginTop: 18, paddingTop: 14, borderTop: "1px solid #edf0f4",
          }}
        >
          <button
            className="primary"
            disabled={
              apply.isPending || changedKeys.length === 0 || Object.keys(errors).length > 0
            }
            onClick={() => apply.mutate()}
          >
            Áp dụng
          </button>
          {result && !hasResultRows && (
            <span style={{ color: "var(--muted)", fontSize: 12 }}>Không có gì thay đổi.</span>
          )}
        </div>

        {result && hasResultRows && (
          <div style={{ marginTop: 14 }}>
            {Object.keys(result.applied).length > 0 && (
              <>
                <p style={{ color: "var(--ok)", margin: "0 0 6px", fontSize: 13 }}>
                  Áp dụng ngay, không cần khởi động lại:
                </p>
                {Object.entries(result.applied).map(([key, value]) => (
                  <div className="kv" key={key}>
                    <span className="k">{settingLabel(key)}</span>
                    <span className="v">{String(value)}</span>
                  </div>
                ))}
              </>
            )}
            {Object.keys(result.restart_required).length > 0 && (
              <>
                <p style={{ color: "var(--warn)", margin: "12px 0 6px", fontSize: 13 }}>
                  {running
                    ? "Đã ghi vào config, cần khởi động lại workspace:"
                    : "Đã lưu, có hiệu lực khi workspace chạy:"}
                </p>
                {Object.entries(result.restart_required).map(([key, reason]) => (
                  <div className="kv" key={key}>
                    <span className="k">{settingLabel(key)}</span>
                    <span className="v">{reason}</span>
                  </div>
                ))}
              </>
            )}
            {Object.keys(result.rejected).length > 0 && (
              <>
                <p style={{ color: "var(--bad)", margin: "12px 0 6px", fontSize: 13 }}>
                  Bị từ chối:
                </p>
                {Object.entries(result.rejected).map(([key, reason]) => (
                  <div className="kv" key={key}>
                    <span className="k">{settingLabel(key)}</span>
                    <span className="v">{reason}</span>
                  </div>
                ))}
              </>
            )}
          </div>
        )}
      </section>

      <FollowUpEditor accountId={accountId} />
    </div>
  );
}

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ScenarioExample, ScenarioSettings } from "../lib/api";
import { api } from "../lib/api";
import { Shipping } from "./Shipping";

const MAX_EXAMPLES = 20;
const MAX_FIELD_CHARACTERS = 500;
// Mirrors agent/reply_generator.py MAX_STYLE_RULES / MAX_STYLE_RULE_CHARACTERS.
const MAX_STYLE_RULES = 20;
const MAX_STYLE_RULE_CHARACTERS = 300;

const EMPTY: ScenarioExample = { customer: "", agent: "" };

function styleLines(text: string): string[] {
  return text.split("\n").map((line) => line.trim()).filter(Boolean);
}

function sameExamples(a: ScenarioExample[], b: ScenarioExample[]): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

export function Scenario({ accountId }: { accountId: string }) {
  const [drafts, setDrafts] = useState<ScenarioExample[]>([]);
  const [dirty, setDirty] = useState(false);
  const [saved, setSaved] = useState(false);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [styleDraft, setStyleDraft] = useState("");
  const [styleIsDefault, setStyleIsDefault] = useState(false);
  const [savedStyle, setSavedStyle] = useState<string[]>([]);
  const [styleSaved, setStyleSaved] = useState(false);
  const [styleError, setStyleError] = useState<string | null>(null);
  const [languageSaved, setLanguageSaved] = useState(false);

  const queryClient = useQueryClient();
  const scenario = useQuery({
    queryKey: ["scenario", accountId],
    queryFn: () => api.scenario(accountId),
  });

  // Only overwrite the cards from the server the first time they load (or
  // when switching account) - once the operator has started editing, a
  // background refetch must never clobber unsaved cards underneath them.
  // At least one empty card is always shown so there is somewhere to type.
  useEffect(() => {
    if (scenario.data === undefined) return;
    setDrafts(scenario.data.examples.length ? scenario.data.examples : [{ ...EMPTY }]);
    setDirty(false);
    setStyleDraft(scenario.data.reply_style.join("\n"));
    setSavedStyle(scenario.data.reply_style);
    setStyleIsDefault(scenario.data.reply_style_is_default);
  }, [scenario.data, accountId]);

  // Saved on change, not behind its own button: one picker with one value has
  // nothing to review before saving.
  const saveLanguage = useMutation({
    mutationFn: (language: string) => api.saveReplyLanguage(accountId, language),
    onSuccess: (data) => {
      queryClient.setQueryData(["scenario", accountId], (previous) =>
        previous === undefined
          ? previous
          // The style rules come back too: rules that follow the language
          // have just changed with it.
          : { ...(previous as ScenarioSettings), ...data },
      );
      setLanguageSaved(true);
      setTimeout(() => setLanguageSaved(false), 3000);
    },
  });

  const saveStyle = useMutation({
    mutationFn: (rules: string[]) => api.saveReplyStyle(accountId, rules),
    onSuccess: (data) => {
      setStyleDraft(data.reply_style.join("\n"));
      setSavedStyle(data.reply_style);
      // Local, not the query cache: refreshing the cache would reset the
      // scenario cards an operator may still be editing.
      setStyleIsDefault(data.reply_style_is_default);
      setStyleSaved(true);
      setTimeout(() => setStyleSaved(false), 3000);
    },
  });

  const handleSaveStyle = () => {
    const rules = styleLines(styleDraft);
    if (rules.length > MAX_STYLE_RULES) {
      setStyleError(`Tối đa ${MAX_STYLE_RULES} quy tắc giọng văn.`);
      return;
    }
    if (rules.some((rule) => rule.length > MAX_STYLE_RULE_CHARACTERS)) {
      setStyleError(`Mỗi quy tắc tối đa ${MAX_STYLE_RULE_CHARACTERS} ký tự.`);
      return;
    }
    setStyleError(null);
    saveStyle.mutate(rules);
  };

  const styleChanged =
    JSON.stringify(styleLines(styleDraft)) !== JSON.stringify(savedStyle);

  const save = useMutation({
    mutationFn: (examples: ScenarioExample[]) => api.saveScenario(accountId, examples),
    onSuccess: (data) => {
      setDrafts(data.examples.length ? data.examples : [{ ...EMPTY }]);
      setDirty(false);
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
    },
  });

  const updateCard = (index: number, field: keyof ScenarioExample, value: string) => {
    setDrafts((previous) =>
      previous.map((card, i) => (i === index ? { ...card, [field]: value } : card)),
    );
    setDirty(true);
    setValidationError(null);
  };

  const removeCard = (index: number) => {
    setDrafts((previous) => {
      const next = previous.filter((_, i) => i !== index);
      return next.length ? next : [{ ...EMPTY }];
    });
    setDirty(true);
    setValidationError(null);
  };

  const addCard = () => {
    setDrafts((previous) => [...previous, { ...EMPTY }]);
  };

  const handleSave = () => {
    const cleaned: ScenarioExample[] = [];
    for (const card of drafts) {
      const customer = card.customer.trim();
      const agent = card.agent.trim();
      if (!customer && !agent) continue; // an untouched blank card, silently dropped
      if (!customer || !agent) {
        setValidationError(
          "Mỗi kịch bản cần cả câu Khách hỏi và câu Agent trả lời - hoặc để trống cả hai.",
        );
        return;
      }
      if (customer.length > MAX_FIELD_CHARACTERS || agent.length > MAX_FIELD_CHARACTERS) {
        setValidationError(`Mỗi ô tối đa ${MAX_FIELD_CHARACTERS} ký tự.`);
        return;
      }
      cleaned.push({ customer, agent });
    }
    if (cleaned.length > MAX_EXAMPLES) {
      setValidationError(`Tối đa ${MAX_EXAMPLES} kịch bản.`);
      return;
    }
    setValidationError(null);
    save.mutate(cleaned);
  };

  // While a save is in flight the picker shows what the operator just chose,
  // so it never flickers back to the old language and then forward again.
  const language =
    (saveLanguage.isPending ? saveLanguage.variables : undefined)
    ?? scenario.data?.reply_language
    ?? "";
  const languageOptions = scenario.data?.reply_languages ?? [];

  const savedExamples = scenario.data?.examples ?? [];
  const hasUnsavedChanges = dirty && !sameExamples(
    drafts.filter((card) => card.customer.trim() || card.agent.trim()),
    savedExamples,
  );

  return (
    <div className="main">
      <header>
        <h2>Kịch bản</h2>
      </header>
      <p className="sub">
        Agent luôn tự sinh câu trả lời bằng AI theo từng tin nhắn của khách. Giọng
        văn và kịch bản dưới đây chỉ là gợi ý để AI bám theo - agent không gửi
        nguyên văn câu mẫu, và vẫn không được bịa giá, tồn kho, đơn hàng hay chính
        sách nếu không có dữ liệu đã xác minh. Lưu được ngay cả khi agent chưa
        chạy; nếu agent đang chạy thì áp dụng luôn, không cần khởi động lại.
      </p>

      <section className="panel" style={{ marginBottom: 14 }}>
        <h3 style={{ margin: 0 }}>Ngôn ngữ trả lời</h3>
        <div style={{ color: "var(--muted)", fontSize: 13, margin: "6px 0 8px" }}>
          Agent luôn trả lời bằng ngôn ngữ này, kể cả khi khách viết thứ tiếng khác.
        </div>
        {saveLanguage.isError && (
          <div className="banner bad">
            {saveLanguage.error instanceof Error
              ? saveLanguage.error.message
              : "Lưu ngôn ngữ thất bại."}
          </div>
        )}
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <select
            value={language}
            disabled={scenario.isLoading || saveLanguage.isPending}
            onChange={(event) => saveLanguage.mutate(event.target.value)}
            style={{ fontSize: 14, padding: 8 }}
          >
            {languageOptions.map((option) => (
              <option key={option.code} value={option.code}>
                {option.label}
              </option>
            ))}
          </select>
          {languageSaved && (
            <span style={{ color: "var(--ok)", fontSize: 12 }}>Đã lưu</span>
          )}
        </div>
      </section>

      <section className="panel" style={{ marginBottom: 14 }}>
        <h3 style={{ margin: 0 }}>Giọng văn</h3>
        <div style={{ color: "var(--muted)", fontSize: 13, margin: "6px 0 8px" }}>
          Mỗi dòng một quy tắc (cách xưng hô, độ dài câu, có dùng emoji hay không...).
          Xoá hết để AI tự chọn giọng.
          {styleIsDefault && (
            <> Đang dùng bộ mặc định của ngôn ngữ trả lời - tự đổi theo khi đổi ngôn ngữ.</>
          )}
        </div>
        {(styleError || saveStyle.isError) && (
          <div className="banner bad">
            {styleError
              ?? (saveStyle.error instanceof Error ? saveStyle.error.message : "Lưu thất bại.")}
          </div>
        )}
        <textarea
          value={styleDraft}
          disabled={scenario.isLoading}
          onChange={(event) => {
            setStyleDraft(event.target.value);
            setStyleError(null);
          }}
          placeholder="Ví dụ: Gọi khách là 'anh/chị', tự xưng 'em'."
          rows={6}
          style={{
            width: "100%", maxWidth: 640, resize: "vertical", fontFamily: "inherit",
            fontSize: 14, padding: 10, boxSizing: "border-box",
          }}
        />
        <div style={{ display: "flex", gap: 10, alignItems: "center", marginTop: 8 }}>
          <button
            className="primary"
            disabled={scenario.isLoading || saveStyle.isPending || !styleChanged}
            onClick={handleSaveStyle}
          >
            Lưu giọng văn
          </button>
          <span style={{ color: "var(--muted)", fontSize: 12 }}>
            {styleLines(styleDraft).length}/{MAX_STYLE_RULES} quy tắc
          </span>
          {styleSaved && !styleChanged && (
            <span style={{ color: "var(--ok)", fontSize: 12 }}>Đã lưu</span>
          )}
        </div>
      </section>

      <Shipping accountId={accountId} />

      <h3>Kịch bản mẫu</h3>
      <div style={{ color: "var(--muted)", fontSize: 13, marginBottom: 10 }}>
        Từng khung là một ví dụ: Khách nói gì, Agent nên trả lời theo hướng nào. AI
        dùng làm mẫu tham khảo rồi viết câu trả lời phù hợp với hội thoại thật.
      </div>

      {scenario.isError && (
        <div className="banner bad">Không tải được kịch bản hiện tại.</div>
      )}
      {(validationError || save.isError) && (
        <div className="banner bad">
          {validationError
            ?? (save.error instanceof Error ? save.error.message : "Lưu thất bại.")}
        </div>
      )}

      <div className="grid" style={{ gap: 14 }}>
        {drafts.map((card, index) => (
          <section className="panel" key={index}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
              <h3 style={{ margin: 0 }}>Kịch bản {index + 1}</h3>
              <button
                type="button"
                className="danger"
                onClick={() => removeCard(index)}
                aria-label={`Xoá kịch bản ${index + 1}`}
              >
                Xoá
              </button>
            </div>

            <label style={{ display: "block", margin: "10px 0 4px", fontSize: 13, color: "var(--muted)" }}>
              Khách
            </label>
            <textarea
              value={card.customer}
              disabled={scenario.isLoading}
              onChange={(event) => updateCard(index, "customer", event.target.value)}
              placeholder="Ví dụ: Sản phẩm này còn size M không shop?"
              rows={2}
              style={{
                width: "100%", maxWidth: 640, resize: "vertical", fontFamily: "inherit",
                fontSize: 14, padding: 10, boxSizing: "border-box",
              }}
            />

            <label style={{ display: "block", margin: "10px 0 4px", fontSize: 13, color: "var(--muted)" }}>
              Agent
            </label>
            <textarea
              value={card.agent}
              disabled={scenario.isLoading}
              onChange={(event) => updateCard(index, "agent", event.target.value)}
              placeholder="Ví dụ: Dạ shop kiểm tra giúp bạn liền ạ, bạn đợi shop xíu nha."
              rows={2}
              style={{
                width: "100%", maxWidth: 640, resize: "vertical", fontFamily: "inherit",
                fontSize: 14, padding: 10, boxSizing: "border-box",
              }}
            />
            <div style={{ color: "var(--muted)", fontSize: 12, marginTop: 4 }}>
              {Math.max(card.customer.length, card.agent.length)}/{MAX_FIELD_CHARACTERS} ký tự
            </div>
          </section>
        ))}
      </div>

      <div style={{ display: "flex", gap: 10, alignItems: "center", marginTop: 14 }}>
        <button
          type="button"
          disabled={drafts.length >= MAX_EXAMPLES}
          onClick={addCard}
        >
          + Thêm kịch bản
        </button>
        <button
          className="primary"
          disabled={scenario.isLoading || save.isPending || !hasUnsavedChanges}
          onClick={handleSave}
        >
          Lưu kịch bản
        </button>
        {saved && !hasUnsavedChanges && (
          <span style={{ color: "var(--ok)", fontSize: 12 }}>Đã lưu</span>
        )}
      </div>
    </div>
  );
}

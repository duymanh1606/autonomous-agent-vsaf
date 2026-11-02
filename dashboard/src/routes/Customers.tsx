import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type {
  AgentEvent, Conversation, ConversationControl, ConversationDetail, MessageMedia,
} from "../lib/api";
import { api } from "../lib/api";
import { newClientId } from "../lib/id";
import { PaperclipIcon, PersonIcon, SendIcon, SparklesIcon } from "../components/ui/icons";
import { useToast } from "../components/Toast";

// Canned openers an operator can drop straight into the composer - client-side
// only, no backend endpoint suggests these, so they stay generic rather than
// claiming a promo/policy the shop may not actually run.
const QUICK_REPLIES = [
  "Xin chào bạn, mình có thể giúp gì ạ?",
  "Đơn của bạn đang được xử lý, mình sẽ cập nhật ngay khi có tin mới nhé.",
  "Bạn vui lòng chờ mình kiểm tra trong giây lát.",
  "Cảm ơn bạn đã liên hệ ạ!",
];

// Business stages/intents come from account config (`registered_stages` /
// `registered_intents` in configs/*.yaml), so this list is the current
// vocabulary, not a closed enum - anything unrecognised still gets a
// readable fallback instead of a raw backend string.
const STAGE_LABELS: Record<string, string> = {
  new: "Khách mới",
  browsing: "Xem sản phẩm",
  asking_product: "Hỏi sản phẩm",
  asking_price: "Hỏi giá",
  checking_inventory: "Kiểm tra tồn kho",
  interested: "Quan tâm sản phẩm",
  ordering: "Đang đặt hàng",
  after_sales: "Hậu mãi",
  complaint: "Khiếu nại",
  human_required: "Cần hỗ trợ",
  closed: "Đã đóng",
};

const INTENT_LABELS: Record<string, string> = {
  greeting: "Chào hỏi",
  faq: "Hỏi đáp",
  ask_product: "Hỏi sản phẩm",
  ask_price: "Hỏi giá",
  check_inventory: "Kiểm tra tồn kho",
  ask_shipping: "Hỏi vận chuyển",
  ask_order: "Hỏi đơn hàng",
  order_status: "Trạng thái đơn hàng",
  complaint: "Khiếu nại",
  return_request: "Yêu cầu đổi trả",
  thank_you: "Cảm ơn",
  unknown: "Chưa xác định",
};

function humanize(value: string): string {
  return value
    .split(/[_-]/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function stageLabel(stage: string | null): string {
  if (!stage) return "Chưa phân loại";
  return STAGE_LABELS[stage] ?? humanize(stage);
}

function intentLabel(intent: string | null): string | null {
  if (!intent) return null;
  return INTENT_LABELS[intent] ?? humanize(intent);
}

type Presence = {
  kind: "auto" | "human" | "waiting" | "escalated";
  label: string;
  dot: string;
};

// The card and the header both need this, and it must reflect exactly what
// the takeover/release buttons act on - control.mode is the source of truth,
// requires_human is only a softer "the AI flagged this" signal on top of it.
function presenceOf(control: ConversationControl, requiresHuman: boolean | null): Presence {
  if (control.mode === "HUMAN") return { kind: "human", label: "Nhân viên đang xử lý", dot: "🔵" };
  if (control.mode === "WAITING_HUMAN") return { kind: "waiting", label: "Chờ tiếp quản", dot: "🟠" };
  if (requiresHuman) return { kind: "escalated", label: "Cần hỗ trợ", dot: "🔴" };
  return { kind: "auto", label: "Agent đang xử lý", dot: "🟢" };
}

function initials(name: string): string {
  const trimmed = name.trim();
  if (!trimmed) return "?";
  return trimmed.charAt(0).toUpperCase();
}

// TikTok avatar URLs carry a signed, expiring token (same pattern as most
// social platforms) - one that has expired 404s/errors instead of loading,
// so this always falls back to the initials avatar on error rather than
// leaving a broken image icon in the UI.
function Avatar({ name, avatarUrl }: { name: string; avatarUrl?: string | null }) {
  const [broken, setBroken] = useState(false);
  if (avatarUrl && !broken) {
    return (
      <img
        className="customer-avatar customer-avatar-img"
        src={avatarUrl}
        alt=""
        referrerPolicy="no-referrer"
        onError={() => setBroken(true)}
      />
    );
  }
  return <span className="customer-avatar">{initials(name)}</span>;
}

function displayNameOf(conversation: Conversation): string {
  return (
    conversation.display_name ||
    conversation.username ||
    conversation.customer_id ||
    conversation.conversation_id.slice(-12)
  );
}

function shortTime(iso: string): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  if (date.toDateString() === now.toDateString()) {
    return date.toLocaleTimeString("vi-VN", { hour: "2-digit", minute: "2-digit" });
  }
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) return "Hôm qua";
  return date.toLocaleDateString("vi-VN", { day: "2-digit", month: "2-digit" });
}

function bubbleTime(iso: string): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString("vi-VN", { hour: "2-digit", minute: "2-digit" });
}

type StatusFilter = "all" | "auto" | "human" | "waiting" | "escalated";
// "unclassified" is its own value, not a stage string - a conversation the
// agent has not analysed yet (stage: null) is a real, common state (a
// customer who just wrote in, before the first reply), not an edge case to
// hide.
type StageFilter = "all" | "unclassified" | string;
type SortOrder = "newest" | "oldest";

function CustomerCard({
  conversation,
  active,
  onSelect,
}: {
  conversation: Conversation;
  active: boolean;
  onSelect: () => void;
}) {
  const presence = presenceOf(conversation.control, conversation.requires_human);
  const name = displayNameOf(conversation);
  return (
    <button
      className={`customer-card ${active ? "active" : ""}`}
      onClick={onSelect}
    >
      <div className="customer-card-top">
        <Avatar name={name} avatarUrl={conversation.avatar_url} />
        <div className="customer-card-heading">
          <div className="customer-card-name-row">
            <span className="customer-card-name">{name}</span>
            <span className="customer-card-time">{shortTime(conversation.last_message_at)}</span>
          </div>
          <span className="customer-card-status">{stageLabel(conversation.stage)}</span>
        </div>
      </div>
      <div className="customer-card-bottom">
        <span className="customer-card-meta">💬 {conversation.message_count} tin</span>
        <span className={`presence-pill ${presence.kind}`}>
          {presence.dot} {presence.label}
        </span>
      </div>
    </button>
  );
}

function CustomerCardSkeleton() {
  return (
    <>
      {[0, 1, 2, 3].map((index) => (
        <div className="customer-card skeleton" key={index} />
      ))}
    </>
  );
}

/** Where a bubble's picture actually comes from, or "" when nowhere.
 *
 * A local file wins over TikTok's own URL: `path` is a file on the server's
 * disk - an agent attachment out of the product catalogue, or an inbound
 * picture `services/media_cache.py` downloaded - served through an
 * account-scoped endpoint, so it neither expires nor needs a TikTok session.
 * `src` is only a fallback: those URLs are signed with `x-expires` about a
 * month out, and DM pictures live on a `-private` host that answers 403 to a
 * browser without the session cookies. `blob:` is never usable here at all -
 * it resolves only inside the Chrome tab that created it.
 */
function mediaUrl(accountId: string, item: MessageMedia): string {
  if (item.path) return api.conversationMediaUrl(accountId, item.path);
  const src = item.src || "";
  return src.startsWith("blob:") ? "" : src;
}

function MessageBubble({
  message, accountId, translation, onDismiss,
}: {
  message: ConversationDetail["messages"][number];
  accountId: string;
  // Bản dịch tiếng Việt của tin này, khi shop không trả lời bằng tiếng Việt.
  translation?: string;
  // Only ever passed for a failed send: it exists nowhere but this page's
  // own state, so dismissing it is a local removal, not a delete against
  // the customer's thread on TikTok.
  onDismiss?: () => void;
}) {
  const direction = message.direction === "OUTBOUND" ? "outbound" : "inbound";
  const attached = message.media || [];
  const media = attached
    .map((item) => ({ item, url: mediaUrl(accountId, item) }))
    .filter((entry) => entry.url);
  // Everything this bubble carried turned out to be undisplayable - a `blob:`
  // URL, or a CDN URL with no local copy behind it. Saying so beats a bubble
  // that looks empty for no reason.
  const mediaUnavailable = attached.length > 0 && media.length === 0;
  return (
    <div className={`message-row ${direction}`}>
      <div className="message-col">
        <div className={`message-bubble ${message.failed ? "failed" : ""}`}>
          {media.length > 0 && (
            <div className="message-media">
              {media.map(({ item, url }, index) =>
                item.kind === "video" ? (
                  <video key={index} src={url} controls onClick={(event) => event.stopPropagation()} />
                ) : (
                  <img
                    key={index}
                    src={url}
                    alt={item.name || "Hình ảnh"}
                    onClick={() => window.open(url, "_blank", "noopener,noreferrer")}
                  />
                ),
              )}
            </div>
          )}
          {mediaUnavailable && (
            <span className="message-text">
              <em style={{ opacity: 0.7 }}>(ảnh không còn hiển thị được)</em>
            </span>
          )}
          {(message.text || (media.length === 0 && !mediaUnavailable)) && (
            <span className="message-text">
              {message.text || <em style={{ opacity: 0.7 }}>(không có chữ)</em>}
            </span>
          )}
          {translation && (
            <span className="message-translation" lang="vi" title="Bản dịch máy - chỉ để đọc, khách không thấy">
              {translation}
            </span>
          )}
        </div>
        <span className={`message-time ${message.failed ? "failed" : ""}`}>
          {message.failed
            ? message.uncertain
              ? "Chưa xác nhận được - kiểm tra trên TikTok trước khi gửi lại"
              : "Gửi thất bại - tin nhắn chưa đến khách"
            : message.source === "unverified"
              ? `${bubbleTime(message.created_at)} · chưa xác minh - kiểm tra trên TikTok`
              : bubbleTime(message.created_at)}
          {message.failed && onDismiss && (
            <button type="button" className="message-dismiss" onClick={onDismiss}>
              Xoá
            </button>
          )}
        </span>
      </div>
    </div>
  );
}

// A file-only send's bubble text, before and after the agent's verdict.
const SENDING_ATTACHMENT = "📎 Đang gửi tệp đính kèm...";
const ATTACHMENT_LABEL = "📎 Tệp đính kèm";

type SendVariables = { text: string; clientMessageId: string; attachments: string[] };

type PendingAttachment = {
  key: string;
  file: File;
  kind: "image" | "video";
  previewUrl: string | null;
  status: "uploading" | "ready" | "error";
  id?: string;
  error?: string;
};

// The same types and limits the server accepts (dashboard_api/routes/chat_media.py).
const IMAGE_TYPES = /\.(jpe?g|png|webp|gif)$/i;
const VIDEO_TYPES = /\.(mp4|mov|webm)$/i;
const MAX_IMAGE_MB = 10;
const MAX_VIDEO_MB = 25;

function attachmentKind(file: File): "image" | "video" | null {
  if (IMAGE_TYPES.test(file.name)) return "image";
  if (VIDEO_TYPES.test(file.name)) return "video";
  return null;
}

export function MessageComposer({
  accountId,
  enabled,
  disabledReason,
  onSend,
  sending,
  draft,
  onDraftChange,
}: {
  accountId: string;
  enabled: boolean;
  disabledReason: string;
  onSend: (text: string, attachmentIds: string[]) => void;
  sending: boolean;
  draft: string;
  onDraftChange: (value: string) => void;
}) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [attached, setAttached] = useState<PendingAttachment[]>([]);
  const [attachError, setAttachError] = useState<string | null>(null);

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 150)}px`;
  }, [draft]);

  const clearAttached = () => {
    setAttached((current) => {
      current.forEach((item) => item.previewUrl && URL.revokeObjectURL(item.previewUrl));
      return [];
    });
  };

  const pickFile = async (file: File) => {
    setAttachError(null);
    const kind = attachmentKind(file);
    if (!kind) {
      setAttachError("Chỉ gửi được ảnh (jpg, png, webp, gif) hoặc video (mp4, mov, webm).");
      return;
    }
    const limitMb = kind === "image" ? MAX_IMAGE_MB : MAX_VIDEO_MB;
    if (file.size > limitMb * 1024 * 1024) {
      setAttachError(`${kind === "image" ? "Ảnh" : "Video"} tối đa ${limitMb} MB.`);
      return;
    }
    if (attached.some((item) => item.kind === kind)) {
      setAttachError("Mỗi tin gửi được một ảnh và một video.");
      return;
    }
    const key = `${Date.now()}-${file.name}`;
    const previewUrl = kind === "image" ? URL.createObjectURL(file) : null;
    setAttached((current) => [...current, { key, file, kind, previewUrl, status: "uploading" }]);
    try {
      const uploaded = await api.uploadChatAttachment(accountId, file);
      setAttached((current) => current.map((item) =>
        item.key === key ? { ...item, status: "ready", id: uploaded.id } : item));
    } catch (err) {
      setAttached((current) => current.map((item) =>
        item.key === key ? { ...item, status: "error", error: (err as Error).message } : item));
    }
  };

  const removeAttached = (key: string) =>
    setAttached((current) => current.filter((item) => {
      if (item.key === key && item.previewUrl) URL.revokeObjectURL(item.previewUrl);
      return item.key !== key;
    }));

  const uploading = attached.some((item) => item.status === "uploading");
  const failed = attached.some((item) => item.status === "error");
  const readyIds = attached.filter((item) => item.status === "ready").map((item) => item.id!);
  const canSend =
    enabled && !sending && !uploading && !failed && (draft.trim().length > 0 || readyIds.length > 0);

  const submit = () => {
    if (!canSend) return;
    onSend(draft.trim(), readyIds);
    onDraftChange("");
    clearAttached();
  };

  return (
    <div className="composer">
      {!enabled && <p className="composer-disabled-hint">{disabledReason}</p>}
      {attached.length > 0 && (
        <ul className="composer-attachments" aria-label="Tệp đính kèm">
          {attached.map((item) => (
            <li key={item.key} className={`composer-attachment is-${item.status}`}>
              {item.previewUrl
                ? <img src={item.previewUrl} alt="" />
                : <span className="composer-attachment-video" aria-hidden="true">▶</span>}
              <span className="composer-attachment-name">
                {item.file.name}
                <small>
                  {item.status === "uploading" && "Đang tải lên..."}
                  {item.status === "ready" && `${(item.file.size / 1024 / 1024).toFixed(1)} MB`}
                  {item.status === "error" && item.error}
                </small>
              </span>
              <button type="button" aria-label={`Bỏ ${item.file.name}`} onClick={() => removeAttached(item.key)}>
                ×
              </button>
            </li>
          ))}
        </ul>
      )}
      {attachError && <p className="composer-attach-error" role="alert">{attachError}</p>}
      <div className="composer-row">
        <input
          ref={fileRef}
          type="file"
          hidden
          accept="image/jpeg,image/png,image/webp,image/gif,video/mp4,video/quicktime,video/webm"
          aria-label="Chọn tệp đính kèm"
          onChange={(event) => {
            const file = event.target.files?.[0];
            event.target.value = "";
            if (file) void pickFile(file);
          }}
        />
        <button
          type="button"
          className="composer-attach"
          aria-label="Đính kèm ảnh hoặc video"
          disabled={!enabled || sending || attached.length >= 2}
          onClick={() => fileRef.current?.click()}
        >
          <PaperclipIcon width={17} height={17} />
        </button>
        <div className={`composer-box ${!enabled ? "disabled" : ""}`}>
          <textarea
            ref={textareaRef}
            className="composer-textarea"
            placeholder="Nhập tin nhắn trả lời khách…"
            value={draft}
            // The send skill refuses longer messages (platforms/tiktok/
            // messaging.py); stopping here beats a "Gửi thất bại" later.
            maxLength={MAX_MESSAGE_CHARACTERS}
            disabled={!enabled || sending}
            onChange={(event) => onDraftChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                submit();
              }
            }}
          />
          {draft.length > MAX_MESSAGE_CHARACTERS * 0.9 && (
            <span className="composer-counter" style={{ fontSize: 11, color: "var(--muted)" }}>
              {draft.length}/{MAX_MESSAGE_CHARACTERS}
            </span>
          )}
        </div>
        <button
          type="button"
          className="primary composer-send"
          disabled={!canSend}
          onClick={submit}
        >
          {sending ? (
            "Đang gửi..."
          ) : (
            <>
              <SendIcon width={15} height={15} />
              Gửi
            </>
          )}
        </button>
      </div>
    </div>
  );
}

// Shared by the always-visible third column (wide screens) and the overlay
// drawer it falls back to below ~1180px - one body, two wrappers, so the two
// never drift out of sync.
function CustomerDetailContent({
  conversation,
  detail,
  onQuickReply,
}: {
  conversation: Conversation | null;
  detail: ConversationDetail;
  onQuickReply: (text: string) => void;
}) {
  const presence = presenceOf(detail.control, conversation?.requires_human ?? null);
  const name = conversation ? displayNameOf(conversation) : detail.conversation_id;
  const profileEntries = Object.entries(detail.profile ?? {}).filter(
    ([, value]) => value !== null && value !== undefined && value !== "",
  );

  return (
    <>
      <div className="customer-detail-section">
        <div className="customer-detail-label">Tài khoản</div>
        <div className="customer-detail-value">{name}</div>
      </div>
      {conversation?.username && (
        <div className="customer-detail-section">
          <div className="customer-detail-label">TikTok</div>
          <div className="customer-detail-value mono">@{conversation.username}</div>
        </div>
      )}
      <div className="customer-detail-section">
        <div className="customer-detail-label">Giai đoạn</div>
        <div className="customer-detail-value">{stageLabel(conversation?.stage ?? null)}</div>
      </div>
      <div className="customer-detail-section">
        <div className="customer-detail-label">Trạng thái xử lý</div>
        <span className={`presence-pill ${presence.kind}`}>
          {presence.dot} {presence.label}
        </span>
      </div>
      {detail.control.operator_id && (
        <div className="customer-detail-section">
          <div className="customer-detail-label">Người tiếp quản</div>
          <div className="customer-detail-value">{detail.control.operator_id}</div>
        </div>
      )}
      {detail.control.reason && (
        <div className="customer-detail-section">
          <div className="customer-detail-label">Lý do</div>
          <div className="customer-detail-value">{detail.control.reason}</div>
        </div>
      )}

      <div className="customer-quick-replies">
        <div className="customer-detail-label">Câu trả lời nhanh</div>
        <div className="quick-reply-list">
          {QUICK_REPLIES.map((text) => (
            <button key={text} className="quick-reply-chip" onClick={() => onQuickReply(text)}>
              {text}
            </button>
          ))}
        </div>
      </div>

      {profileEntries.length > 0 && (
        <div className="customer-info-section">
          <div className="customer-info-section-title">Hồ sơ</div>
          {profileEntries.map(([key, value]) => (
            <div className="kv" key={key}>
              <span className="k">{key}</span>
              <span className="v prose">{String(value)}</span>
            </div>
          ))}
        </div>
      )}

      {detail.facts.length > 0 && (
        <div className="customer-info-section">
          <div className="customer-info-section-title">Thông tin đã thu thập</div>
          {detail.facts.map((fact) => (
            <div className="kv" key={fact.fact_type}>
              <span className="k">{fact.fact_type}</span>
              <span className="v prose">
                {fact.fact_value}
                {fact.confidence != null && (
                  <span style={{ color: "var(--muted)" }}>
                    {" "}
                    ({Math.round(fact.confidence * 100)}%)
                  </span>
                )}
              </span>
            </div>
          ))}
        </div>
      )}

      {typeof detail.state.conversation_summary === "string" &&
        detail.state.conversation_summary && (
          <div className="customer-info-section">
            <div className="customer-info-section-title">Tóm tắt</div>
            <p style={{ margin: 0, fontSize: 13, color: "var(--ink)" }}>
              {String(detail.state.conversation_summary)}
            </p>
          </div>
        )}

      {typeof detail.state.intent === "string" && detail.state.intent && (
        <div className="customer-info-section">
          <div className="customer-info-section-title">Ý định gần nhất</div>
          <p style={{ margin: 0, fontSize: 13, color: "var(--ink)" }}>
            {intentLabel(String(detail.state.intent))}
          </p>
        </div>
      )}
    </>
  );
}

// The longest message the send skill accepts (platforms/tiktok/messaging.py).
const MAX_MESSAGE_CHARACTERS = 1000;

export function Customers({
  accountId, focusConversationId, onFocused, events = [],
}: {
  accountId: string;
  // Set by Alerts/the command palette to jump straight to one conversation -
  // applied once, then cleared via onFocused so it doesn't re-fire.
  focusConversationId?: string | null;
  onFocused?: () => void;
  // The agent publishes `conversation.message.failed` when a manual send is
  // never verified on TikTok (core/graph.py's _graph_finalize); nothing is
  // written to the database in that case, so the event is the only way this
  // page learns the message did not go out.
  events?: AgentEvent[];
}) {
  const [open, setOpen] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [stageFilter, setStageFilter] = useState<StageFilter>("all");
  const [sortOrder, setSortOrder] = useState<SortOrder>("newest");
  const [draft, setDraft] = useState("");
  const client = useQueryClient();
  const toast = useToast();

  const list = useQuery({
    queryKey: ["conversations", accountId],
    queryFn: () => api.conversations(accountId),
    refetchInterval: 15000,
  });

  const detail = useQuery({
    queryKey: ["conversation", accountId, open],
    queryFn: () => api.conversation(accountId, open!),
    enabled: Boolean(open),
    // A robustness fallback so the open conversation keeps updating even if
    // a WebSocket event is missed - correctness must not depend on it.
    refetchInterval: 5000,
  });

  // Keyed on the newest message so a new one is translated as it arrives,
  // while the 5s poll above, which changes nothing, costs nothing here.
  const newestMessage = detail.data?.messages[detail.data.messages.length - 1];
  const newestKey = newestMessage ? `${newestMessage.message_id}:${newestMessage.direction}` : "";
  const translations = useQuery({
    queryKey: ["conversation-translations", accountId, open, newestKey],
    queryFn: () => api.conversationTranslations(accountId, open!),
    enabled: Boolean(open) && Boolean(newestKey),
    staleTime: Infinity,
    retry: false,
    // Keep the old translations on screen while a new message is translated,
    // but never carry one conversation's over into another.
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey[2] === open ? previous : undefined,
    // A long thread is translated a batch per request, newest first.
    refetchInterval: (query) =>
      query.state.data && query.state.data.remaining > 0 && !query.state.data.error ? 1500 : false,
  });
  const translationByKey = useMemo(() => {
    const map = new Map<string, string>();
    for (const item of translations.data?.translations ?? []) {
      map.set(`${item.message_id}:${item.direction}`, item.text);
    }
    return map;
  }, [translations.data]);

  // Only the stages actually present in this account's conversations - a
  // fixed 11-entry list (STAGE_LABELS) would dwarf a shop that only ever
  // uses 3 or 4 of them. Ordered to match STAGE_LABELS' own declaration
  // order (roughly the customer journey) with any custom, non-standard
  // stage from account config appended after.
  const availableStages = useMemo(() => {
    const seen = new Set<string>();
    let hasUnclassified = false;
    for (const conversation of list.data ?? []) {
      if (conversation.stage) seen.add(conversation.stage);
      else hasUnclassified = true;
    }
    const ordered = Object.keys(STAGE_LABELS).filter((stage) => seen.has(stage));
    for (const stage of seen) {
      if (!ordered.includes(stage)) ordered.push(stage);
    }
    return { stages: ordered, hasUnclassified };
  }, [list.data]);

  const filtered = useMemo(() => {
    const all = list.data ?? [];
    const term = search.trim().toLowerCase();
    let rows = all.filter((conversation) => {
      if (term) {
        const haystack = [
          conversation.display_name,
          conversation.username,
          conversation.customer_id,
          conversation.conversation_id,
        ]
          .filter(Boolean)
          .join(" ")
          .toLowerCase();
        if (!haystack.includes(term)) return false;
      }
      if (statusFilter !== "all") {
        const presence = presenceOf(conversation.control, conversation.requires_human);
        if (presence.kind !== statusFilter) return false;
      }
      if (stageFilter === "unclassified") {
        if (conversation.stage) return false;
      } else if (stageFilter !== "all") {
        if (conversation.stage !== stageFilter) return false;
      }
      return true;
    });
    rows = [...rows].sort((a, b) => {
      const diff = new Date(a.last_message_at).getTime() - new Date(b.last_message_at).getTime();
      return sortOrder === "newest" ? -diff : diff;
    });
    return rows;
  }, [list.data, search, statusFilter, stageFilter, sortOrder]);

  useEffect(() => {
    setDraft("");
  }, [open]);

  useEffect(() => {
    if (!focusConversationId) return;
    setOpen(focusConversationId);
    onFocused?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusConversationId]);

  const invalidateConversation = () => {
    client.invalidateQueries({ queryKey: ["conversations", accountId] });
    client.invalidateQueries({ queryKey: ["conversation", accountId, open] });
  };

  // Taking over is a decision the operator has already made; the round trip
  // that records it (dashboard -> supervisor -> the agent's control server ->
  // PostgreSQL) is not something they should have to watch. This holds the
  // mode they just chose until the real one catches up, so the header, the
  // presence pill and the composer all switch on the click.
  //
  // Component state, not the query cache, and for the same reason the
  // pending sends below are: every refetch - the 5s poll included - replaces
  // that cache wholesale from the database, so an optimistic value parked
  // there flickers back to the old mode the moment a poll lands in the
  // window before the write commits.
  const [pendingMode, setPendingMode] = useState<"HUMAN" | "AUTO" | null>(null);

  // Anything the operator opens next must show its own real mode, never the
  // one left over from the conversation they were just in.
  useEffect(() => {
    setPendingMode(null);
  }, [open]);

  // Drop the override only once the database agrees, not when the request
  // returns: `invalidateConversation` starts a refetch, and clearing before
  // it lands would show the old mode again for exactly as long as that
  // refetch takes - the flicker this whole mechanism exists to avoid.
  const serverMode = detail.data?.control.mode;
  useEffect(() => {
    if (pendingMode && serverMode === pendingMode) setPendingMode(null);
  }, [pendingMode, serverMode]);

  /** What the operator should see: their pending choice, else the truth. */
  const control = detail.data
    ? { ...detail.data.control, mode: pendingMode ?? detail.data.control.mode }
    : null;

  const takeover = useMutation({
    mutationFn: () => api.takeoverConversation(accountId, open!),
    onMutate: () => setPendingMode("HUMAN"),
    onSuccess: invalidateConversation,
    onError: (err: Error) => {
      // The optimistic mode has to go back, or the composer stays enabled for
      // a conversation the agent still owns and a send would be refused.
      setPendingMode(null);
      toast.error("Không tiếp quản được hội thoại", err.message);
    },
  });

  const release = useMutation({
    mutationFn: () => api.releaseConversation(accountId, open!),
    onMutate: () => setPendingMode("AUTO"),
    onSuccess: invalidateConversation,
    onError: (err: Error) => {
      setPendingMode(null);
      toast.error("Không trả lại được cho Agent", err.message);
    },
  });

  // Sends still on their way, and sends that never made it out. Both live in
  // component state rather than the query cache, and that is the whole point:
  // every refetch (the 5s poll, any invalidate) replaces that cache wholesale
  // from the database, and neither of these has a database row yet - parking
  // an optimistic bubble there made it appear the instant the operator hit
  // send and then vanish a moment later, well before the agent had finished
  // the real send.
  //
  // `id` is the client_message_id given to the API, which is also the
  // message_id the eventual database row carries (TaskRepository.complete in
  // storage/repositories.py writes it verbatim). So a local bubble can be
  // dropped exactly when its real counterpart shows up - no text matching and
  // no timers, both of which get this wrong when a customer or the agent
  // happens to send the same words.
  type LocalSend = {
    id: string; conversationId: string; text: string; at: string; uncertain?: boolean;
  };
  const [pendingSends, setPendingSends] = useState<LocalSend[]>([]);
  const [failedSends, setFailedSends] = useState<LocalSend[]>([]);
  // Read by the events effect below, which would otherwise close over
  // whatever `pendingSends` was when that effect last ran.
  const pendingRef = useRef<LocalSend[]>([]);
  pendingRef.current = pendingSends;

  // Ids the operator has dismissed, so they stay dismissed. The events effect
  // below walks the whole event buffer every time any event arrives, and the
  // hub replays state events to a browser that reconnects - without this, a
  // dismissed bubble came straight back on the next unrelated event.
  const dismissedRef = useRef<Set<string>>(new Set());

  const markFailed = (
    clientMessageId: string, conversationId: string, fallbackText: string, uncertain = false,
  ) => {
    if (dismissedRef.current.has(clientMessageId)) return;
    const match = pendingRef.current.find((item) => item.id === clientMessageId);
    setPendingSends((previous) => previous.filter((item) => item.id !== clientMessageId));
    setFailedSends((previous) =>
      previous.some((item) => item.id === clientMessageId)
        ? previous
        : [...previous, {
            ...(match ?? {
              id: clientMessageId,
              conversationId,
              text: fallbackText,
              at: new Date().toISOString(),
            }),
            // The in-flight placeholder must not keep saying "sending" under
            // a verdict that it failed.
            ...(match?.text === SENDING_ATTACHMENT ? { text: ATTACHMENT_LABEL } : {}),
            uncertain,
          }],
    );
  };

  const send = useMutation({
    mutationFn: ({ text, clientMessageId, attachments }: SendVariables) =>
      api.sendConversationMessage(accountId, open!, text, clientMessageId, attachments),
    // The staff's own message shows up the instant they hit send, and stays
    // put until the agent has actually delivered it - the request returning
    // 200 only means the send was queued, so invalidating the conversation
    // here would refetch a database that does not have the message yet and
    // wipe the bubble off the screen. The `conversation.message.sent` event
    // (core/graph.py's AgentGraph._graph_finalize) is what refetches once the
    // row really exists; the 5s poll is the backstop.
    onMutate: ({ text, clientMessageId, attachments }: SendVariables) => {
      const conversationId = open!;
      // A file-only message still needs something in its bubble until the
      // real row (with the picture) arrives.
      const shown = text || (attachments.length ? SENDING_ATTACHMENT : "");
      setPendingSends((previous) => [...previous, {
        id: clientMessageId, conversationId, text: shown, at: new Date().toISOString(),
      }]);
      return { conversationId };
    },
    // A rejected request never reached the agent, so this send is already
    // final: mark it red instead of dropping the bubble, which would erase
    // what the operator typed and make a failed send look like it was never
    // attempted.
    onError: (err: Error, variables, context) => {
      markFailed(variables.clientMessageId, context?.conversationId ?? open!, variables.text);
      toast.error("Gửi tin thất bại", err.message);
    },
  });

  // A local bubble is done the moment the real row it was standing in for
  // arrives, matched on the id both share.
  useEffect(() => {
    const delivered = new Set((detail.data?.messages ?? []).map((item) => item.message_id));
    if (!delivered.size) return;
    setPendingSends((previous) =>
      previous.some((item) => delivered.has(item.id))
        ? previous.filter((item) => !delivered.has(item.id))
        : previous,
    );
  }, [detail.data]);

  // The request being accepted only means the send was queued; the DOM send
  // is verified later, inside the agent process.
  useEffect(() => {
    for (const event of events) {
      if (event.type !== "conversation.message.failed") continue;
      if (event.account_id !== accountId) continue;
      const conversationId = String(event.payload?.conversation_id ?? "");
      const clientMessageId = String(event.payload?.client_message_id ?? "");
      if (!conversationId || !clientMessageId) continue;
      markFailed(clientMessageId, conversationId, "", event.payload?.uncertain === true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events, accountId]);

  // What this conversation shows on top of the server's messages: sends still
  // in flight, then ones that failed. Anything whose id already came back
  // from the database is dropped here too, not just in the pruning effect,
  // so it cannot flash twice in the render before that effect runs.
  const localBubbles = useMemo(() => {
    const delivered = new Set((detail.data?.messages ?? []).map((item) => item.message_id));
    const mine = (list: LocalSend[]) =>
      list.filter((item) => item.conversationId === open && !delivered.has(item.id));
    return [
      ...mine(pendingSends).map((item) => ({ item, state: "pending" as const })),
      ...mine(failedSends).map((item) => ({ item, state: "failed" as const })),
    ];
  }, [detail.data, pendingSends, failedSends, open]);

  // Auto-scroll: jump to the newest message on open and whenever a new
  // message arrives while already at the bottom; otherwise surface an
  // indicator instead of yanking a user reading older history downward.
  const listRef = useRef<HTMLDivElement>(null);
  const [atBottom, setAtBottom] = useState(true);
  const [showNewIndicator, setShowNewIndicator] = useState(false);
  const prevConversationRef = useRef<string | null>(null);
  // Keys seen on the previous render. "A new message arrived" means the
  // newest one is a key we have not seen - not that the count changed. The
  // count is blind once a thread reaches the fetch window (200): every new
  // message then pushes the oldest out and the length stays put, so nobody
  // was scrolled or told. Dismissing a failed bubble shrinks the count too,
  // which used to read as a new message. Keyed on direction as well because
  // the schema allows one message_id in both directions.
  const seenKeysRef = useRef<Set<string>>(new Set());
  const keyOf = (message: { message_id: string; direction: string }) =>
    `${message.message_id}:${message.direction}`;

  useEffect(() => {
    const el = listRef.current;
    // Local bubbles too: a send the operator just made is the reason to
    // scroll, and it is not in `detail.data` yet.
    const messages = [
      ...(detail.data?.messages ?? []),
      ...localBubbles.map(({ item }) => ({ message_id: item.id, direction: "OUTBOUND" })),
    ];
    if (!el) return;
    const keys = new Set(messages.map(keyOf));
    const newest = messages[messages.length - 1];
    const conversationChanged = prevConversationRef.current !== open;
    if (conversationChanged) {
      prevConversationRef.current = open;
      seenKeysRef.current = keys;
      setShowNewIndicator(false);
      setAtBottom(true);
      requestAnimationFrame(() => {
        el.scrollTop = el.scrollHeight;
      });
      return;
    }
    const arrived = newest !== undefined && !seenKeysRef.current.has(keyOf(newest));
    seenKeysRef.current = keys;
    if (!arrived) return;
    if (atBottom) {
      requestAnimationFrame(() => {
        el.scrollTop = el.scrollHeight;
      });
    } else {
      setShowNewIndicator(true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail.data?.messages, localBubbles, open]);

  // Translations land after the messages and make every bubble taller; an
  // operator reading the newest message must not be left above it.
  useEffect(() => {
    const el = listRef.current;
    if (!el || !atBottom || translationByKey.size === 0) return;
    requestAnimationFrame(() => {
      el.scrollTop = el.scrollHeight;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [translationByKey]);

  const handleScroll = () => {
    const el = listRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    const bottom = distanceFromBottom < 60;
    setAtBottom(bottom);
    if (bottom) setShowNewIndicator(false);
  };

  const jumpToBottom = () => {
    const el = listRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    setShowNewIndicator(false);
    setAtBottom(true);
  };

  const activeConversation = list.data?.find((item) => item.conversation_id === open) ?? null;


  return (
    <div className="inbox-shell">
      <div className={`customer-workspace ${open ? "show-conversation" : ""}`}>
      <section className="customer-list-panel">
        <div className="customer-list-header">
          <div className="customer-list-title-row">
            <h2>
              Khách hàng <span className="customer-count-badge">{filtered.length}</span>
            </h2>
            <span className="customer-total-chip">Tất cả {list.data?.length ?? 0}</span>
          </div>
        </div>

        <div className="customer-search-row">
          <input
            type="search"
            placeholder="🔍 Tìm kiếm khách hàng..."
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </div>

        <div className="customer-filter-panel">
          <select
            aria-label="Lọc theo trạng thái"
            value={statusFilter}
            onChange={(event) => setStatusFilter(event.target.value as StatusFilter)}
          >
            <option value="all">Tất cả trạng thái</option>
            <option value="auto">Agent đang xử lý</option>
            <option value="human">Nhân viên đang xử lý</option>
            <option value="waiting">Chờ tiếp quản</option>
            <option value="escalated">Cần hỗ trợ</option>
          </select>
          <select
            aria-label="Lọc theo phân loại khách hàng"
            value={stageFilter}
            onChange={(event) => setStageFilter(event.target.value)}
          >
            <option value="all">Tất cả phân loại</option>
            {availableStages.stages.map((stage) => (
              <option key={stage} value={stage}>{stageLabel(stage)}</option>
            ))}
            {availableStages.hasUnclassified && (
              <option value="unclassified">Chưa phân loại</option>
            )}
          </select>
          <select
            aria-label="Sắp xếp"
            value={sortOrder}
            onChange={(event) => setSortOrder(event.target.value as SortOrder)}
          >
            <option value="newest">Mới nhất</option>
            <option value="oldest">Cũ nhất</option>
          </select>
        </div>

        <div className="customer-cards">
          {list.isLoading && <CustomerCardSkeleton />}
          {!list.isLoading && (list.data?.length ?? 0) === 0 && (
            <p className="customer-list-empty">Chưa có khách hàng</p>
          )}
          {!list.isLoading && (list.data?.length ?? 0) > 0 && filtered.length === 0 && (
            <p className="customer-list-empty">Không tìm thấy khách hàng phù hợp</p>
          )}
          {filtered.map((conversation) => (
            <CustomerCard
              key={conversation.conversation_id}
              // The card carries the same presence pill as the header, from a
              // different query. Without the override here the two disagree
              // for a beat after a takeover - header says the operator has it,
              // the card still says the agent does.
              conversation={
                control && conversation.conversation_id === open
                  ? { ...conversation, control }
                  : conversation
              }
              active={open === conversation.conversation_id}
              onSelect={() => setOpen(conversation.conversation_id)}
            />
          ))}
        </div>
      </section>

      <section className="conversation-panel">
        {!open && (
          <div className="conversation-empty-state" style={{ gridColumn: "1 / -1", gridRow: "1 / -1" }}>
            Chọn một khách hàng để xem hội thoại
          </div>
        )}

        {open && detail.data && (
          <>
            <div className="conversation-header">
              <div className="conversation-header-identity">
                <button className="conversation-back" onClick={() => setOpen(null)}>
                  ← Quay lại
                </button>
                <Avatar
                  name={activeConversation ? displayNameOf(activeConversation) : "?"}
                  avatarUrl={activeConversation?.avatar_url}
                />
                <div className="conversation-header-text">
                  <div className="conversation-header-name">
                    {activeConversation ? displayNameOf(activeConversation) : open}
                  </div>
                  <div className="conversation-header-sub">
                    <span>
                      {stageLabel(activeConversation?.stage ?? null)} ·{" "}
                      {activeConversation?.message_count ?? detail.data.messages.length} tin nhắn
                    </span>
                    <span className={`presence-pill ${presenceOf(control ?? detail.data.control, activeConversation?.requires_human ?? null).kind}`}>
                      {presenceOf(control ?? detail.data.control, activeConversation?.requires_human ?? null).dot}{" "}
                      {presenceOf(control ?? detail.data.control, activeConversation?.requires_human ?? null).label}
                    </span>
                  </div>
                </div>
              </div>
              <div className="conversation-header-actions">
                {(control ?? detail.data.control).mode === "HUMAN" ? (
                  <>
                    <span className="conversation-agent-pill human">
                      <SparklesIcon width={13} height={13} />
                      Nhân viên đang hỗ trợ
                    </span>
                    <button disabled={release.isPending} onClick={() => release.mutate()}>
                      Trả lại cho Agent
                    </button>
                  </>
                ) : (
                  <>
                    <span className="conversation-agent-pill">
                      <SparklesIcon width={13} height={13} />
                      Agent đang hỗ trợ
                    </span>
                    <button
                      className="primary"
                      disabled={takeover.isPending}
                      onClick={() => takeover.mutate()}
                    >
                      <PersonIcon width={14} height={14} />
                      Tiếp quản
                    </button>
                  </>
                )}
              </div>
            </div>


            {/* `role="log"` marks this as a region where new entries are
                appended and a screen reader should read additions - it is
                polite by default, so a message never interrupts what the
                operator is typing. */}
            <div className="message-list" ref={listRef} onScroll={handleScroll} role="log" aria-label="Tin nhắn trong hội thoại">
              {detail.data.messages.length === 0 && (
                <p className="message-list-empty">Chưa có tin nhắn nào trong hội thoại này</p>
              )}
              {translations.data?.error && (
                <p className="message-translation-note">Bản dịch: {translations.data.error}</p>
              )}
              {detail.data.messages.map((message, index) => (
                <MessageBubble
                  key={`${message.message_id}-${index}`} message={message} accountId={accountId}
                  translation={translationByKey.get(`${message.message_id}:${message.direction}`)}
                />
              ))}
              {/* Local bubbles go after the server's, which is where they
                  belong chronologically - they are the newest thing in the
                  thread. Each is dropped as soon as the same id appears
                  above, so a delivered message is never shown twice. */}
              {localBubbles.map(({ item, state }) => (
                <MessageBubble
                  key={item.id}
                  accountId={accountId}
                  message={{
                    message_id: item.id, direction: "OUTBOUND", text: item.text,
                    created_at: item.at, source: "human", media: [],
                    pending: state === "pending", failed: state === "failed",
                    uncertain: item.uncertain,
                  }}
                  onDismiss={
                    state === "failed"
                      ? () => {
                          dismissedRef.current.add(item.id);
                          setFailedSends((previous) =>
                            previous.filter((entry) => entry.id !== item.id));
                        }
                      : undefined
                  }
                />
              ))}
              {showNewIndicator && (
                <button className="new-message-indicator" onClick={jumpToBottom}>
                  ↓ Tin nhắn mới
                </button>
              )}
            </div>

            <MessageComposer
              accountId={accountId}
              enabled={(control ?? detail.data.control).mode === "HUMAN"}
              disabledReason="Chỉ gửi được khi bạn đang tiếp quản hội thoại"
              sending={send.isPending}
              draft={draft}
              onDraftChange={setDraft}
              onSend={(text, attachments) =>
                send.mutate({ text, attachments, clientMessageId: newClientId() })}
            />
          </>
        )}

        {open && !detail.data && (
          <div className="conversation-empty-state" style={{ gridColumn: "1 / -1", gridRow: "1 / -1" }}>
            Đang tải hội thoại...
          </div>
        )}
      </section>

      {open && detail.data && (
        <aside className="customer-detail-panel">
          <div className="customer-detail-panel-header">
            <h3>Thông tin khách</h3>
          </div>
          <div className="customer-detail-panel-body">
            <CustomerDetailContent
              conversation={activeConversation}
              detail={detail.data}
              onQuickReply={setDraft}
            />
          </div>
        </aside>
      )}

      </div>
    </div>
  );
}

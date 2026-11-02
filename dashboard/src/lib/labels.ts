// Human-readable text for the internal method/capability/event names the
// backend actually emits (platform.*/messaging.* capabilities, agent.*/
// task.*/conversation.* event types, run modes and statuses). The interface
// should never show a raw dotted method name outside of an explicit "kỹ
// thuật"/"raw" opt-in toggle - everything else routes through here so a new
// name still renders (humanized) instead of disappearing.
import type { ActivityRow, AgentEvent, OrderStatus } from "./api";

export const ORDER_STATUS_LABEL: Record<OrderStatus, string> = {
  pending: "Chờ xác nhận",
  packing: "Đang đóng gói",
  shipping: "Đang giao",
  done: "Hoàn tất",
  returned: "Hoàn trả",
  cancelled: "Đã huỷ",
};

// Reuses the same red/amber/blue/green vocabulary as .tag/.presence-pill
// elsewhere in the app instead of inventing a new palette per status.
export const ORDER_STATUS_TINT: Record<OrderStatus, "ok" | "warn" | "bad" | "info"> = {
  pending: "warn",
  packing: "info",
  shipping: "info",
  done: "ok",
  returned: "bad",
  cancelled: "bad",
};

// Mirrors dashboard_api/orders.py STATUS_TRANSITIONS: cancellation is only
// possible before the order reaches the carrier ("shipping"); once shipped
// or in a terminal state the status is locked to that forward path.
export const ORDER_STATUS_TRANSITIONS: Record<OrderStatus, OrderStatus[]> = {
  pending: ["packing", "cancelled"],
  packing: ["shipping", "cancelled"],
  shipping: ["done", "returned"],
  done: [],
  returned: [],
  cancelled: [],
};

export function canCancelOrder(status: OrderStatus): boolean {
  return ORDER_STATUS_TRANSITIONS[status].includes("cancelled");
}

export function isOrderLocked(status: OrderStatus): boolean {
  return status === "shipping" || status === "done" || status === "returned" || status === "cancelled";
}

// Label for the button/option that moves an order *into* this status.
export const ORDER_STATUS_ACTION_LABEL: Record<OrderStatus, string> = {
  pending: "Chờ xác nhận",
  packing: "Đóng gói",
  shipping: "Giao cho ĐVVC",
  done: "Đã giao xong",
  returned: "Đánh dấu hoàn trả",
  cancelled: "Huỷ đơn",
};

export const CAPABILITY_LABEL: Record<string, string> = {
  "messaging.scan_inbox": "Quét hộp thư",
  open_inbox: "Mở hộp thư",
  open_tiktok: "Mở TikTok",
  open_profile: "Mở trang cá nhân",
};

// One runtime. The older "extension" one was removed from the backend, and
// its picker here had been hidden behind SHOW_TECHNICAL_INFO for longer than
// that - an adopted process from before the change still reports whatever
// string it reported, so `runtimeLabel` keeps humanising the unknown.
export const RUNTIME_LABEL: Record<string, string> = {
  langgraph: "LangGraph",
};

export const RUN_MODE_LABEL: Record<string, string> = {
  parallel: "Song song",
  sequential: "Tuần tự",
};

export const RUN_STATUS_LABEL: Record<string, string> = {
  completed: "Hoàn tất",
  failed: "Thất bại",
  pending: "Đang chờ",
  running: "Đang chạy",
  cancelled: "Đã huỷ",
};

const ACTION_LABELS: Record<string, string> = {
  "platform.list_unread_conversations": "Agent quét danh sách hội thoại chưa đọc",
  "platform.open_inbox": "Agent mở Inbox trên TikTok",
  "platform.open_conversation": "Agent mở một hội thoại",
  "platform.send_message": "Agent gửi tin nhắn",
  "platform.focus_message_input": "Agent chọn ô nhập tin nhắn",
  "platform.attach_message_media": "Agent đính kèm ảnh/video",
  "platform.retry_send": "Agent thử gửi lại tin nhắn",
  "platform.open_profile": "Agent mở trang cá nhân TikTok",
  "messaging.scan_inbox": "Agent quét Inbox",
  "messaging.send": "Agent gửi tin nhắn",
  "messaging.reply": "Agent tạo và gửi câu trả lời",
  reply_to_message: "Agent trả lời tin nhắn",
  send_message: "Agent gửi tin nhắn",
  open_inbox: "Agent mở Inbox",
  open_tiktok: "Agent mở TikTok",
  open_profile: "Agent mở trang cá nhân",
};

const EVENT_LABELS: Record<string, string> = {
  "agent.status": "Cập nhật trạng thái agent",
  "agent.paused": "Agent đã tạm dừng",
  "agent.resumed": "Agent đã tiếp tục hoạt động",
  "agent.stopping": "Agent đang dừng lại",
  "health.changed": "Tình trạng hệ thống thay đổi",
  "task.submitted": "Có task mới được giao cho agent",
  "task.started": "Agent bắt đầu xử lý task",
  "task.finished": "Agent hoàn thành task",
  "task.failed": "Task thất bại",
  "escalation.opened": "Một hội thoại cần người xử lý",
  "conversation.takeover": "Nhân viên đã tiếp quản hội thoại",
  "conversation.released": "Hội thoại được trả lại cho Agent",
  "conversation.message.received": "Khách hàng gửi tin nhắn mới",
  "conversation.message.sent": "Đã gửi tin nhắn cho khách",
};

function humanize(value: string): string {
  return value
    .replace(/[._]/g, " ")
    .split(" ")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function actionLabel(row: ActivityRow): string {
  return ACTION_LABELS[row.action_name] ?? ACTION_LABELS[row.capability ?? ""] ?? humanize(row.action_name);
}

export function eventLabel(event: AgentEvent): string {
  return EVENT_LABELS[event.type] ?? humanize(event.type);
}

export function eventDescription(event: AgentEvent): string | null {
  const payload = event.payload ?? {};
  const conversation = payload["conversation"] ?? payload["conversation_id"];
  if (event.type === "conversation.message.received" || event.type === "conversation.message.sent") {
    return conversation ? `Hội thoại với ${conversation}` : null;
  }
  if (event.type === "escalation.opened" || event.type === "conversation.takeover" || event.type === "conversation.released") {
    return conversation ? `Hội thoại ${conversation}` : null;
  }
  if (event.type === "task.failed" && payload["error"]) {
    return String(payload["error"]);
  }
  return null;
}

export function capabilityLabel(value: string | null | undefined): string {
  if (!value) return "—";
  return CAPABILITY_LABEL[value] ?? ACTION_LABELS[value] ?? humanize(value);
}

export function runtimeLabel(value: string | null | undefined): string {
  if (!value) return "Chưa chạy";
  return RUNTIME_LABEL[value] ?? humanize(value);
}

export function runModeLabel(value: string): string {
  return RUN_MODE_LABEL[value] ?? humanize(value);
}

export function runStatusLabel(value: string): string {
  return RUN_STATUS_LABEL[value] ?? humanize(value);
}

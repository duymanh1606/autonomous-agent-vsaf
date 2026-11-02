// One place that knows the supervisor's shape. Everything else works in terms
// of these types, so a backend change surfaces here as a type error rather than
// as a blank panel at runtime.

export type ProcessInfo = { running: boolean; pid: number | null; runtime?: string | null };

/** The dashboard's own operator login - separate from `Account`, which is a
 * TikTok Shop workspace, not a human user. */
export type RegistrationReceipt = {
  ok: boolean;
  pending_approval: boolean;
};

export type User = {
  id: number;
  email: string;
  is_admin: boolean;
  account_ids: string[];
  /** How many workspaces this user may create for themselves. null means an
   * admin has not granted any, and counts as 0 - never as "unlimited".
   * Admins are never capped regardless of this value. */
  max_workspaces: number | null;
  /** Logged in with a mailed temporary password: must set a new one before
   *  anything else works. */
  must_change_password?: boolean;
};

export type UsageTotals = {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  calls: number;
};

export type ManagedUser = {
  id: number;
  email: string;
  is_admin: boolean;
  is_active: boolean;
  /** Inactive and never approved: a signup waiting for an admin, as opposed
   *  to an account an admin deliberately switched off. */
  is_pending: boolean;
  created_at: string;
  account_ids: string[];
  usage: UsageTotals;
  /** The quota an admin granted this user. null = none granted yet, which
   *  behaves as 0; the page shows the two differently ("chưa cấp" vs "0"). */
  max_workspaces: number | null;
  /** Workspaces this user created (owns) - what max_workspaces counts.
   *  Workspaces merely granted to them are in `account_ids` and are not
   *  charged against the quota. */
  owned_workspaces: number;
};

export type Account = {
  id: string;
  label: string;
  index: number;
  cdp_port: number;
  control_port: number;
  // Where this workspace's OWN noVNC web client listens - each workspace has
  // its own dedicated Xvfb/x11vnc/noVNC now, not a screen shared with every
  // other workspace. See openVncWindow().
  novnc_port: number;
  config_path: string;
  profile_dir: string;
  profile_dir_name: string;
  has_api_key: boolean;
  // egress-ip-gateway: one public IP per workspace. has_egw_token is the
  // opt-in switch (the token itself never reaches the frontend); route and
  // expected_ip are filled when enabled from the dashboard, empty for a
  // token pasted by hand.
  has_egw_token: boolean;
  // has_egw_token && the deployment-wide EGW_ENABLED switch: what the
  // workspace will really do at its next Start.
  egw_active: boolean;
  egw_route: string;
  egw_expected_ip: string;
  owner_id: number | null;
  // Only resolved for an admin's own GET /api/accounts response - null for
  // a non-admin (they only ever see their own workspaces anyway) and for
  // legacy workspaces created before ownership existed.
  owner_email: string | null;
  database_path: string;
  processes: { chrome: ProcessInfo; agent: ProcessInfo };
  control_connected: boolean;
  database: { ok: boolean; detail: string };
  // Sleep/wake lifecycle (dashboard_api/sleep_wake.py) - Chrome-only, the
  // agent process itself keeps running through all of these.
  pinned: boolean;
  lifecycle_state: "active" | "idle" | "sleeping" | "waking" | "recovery" | "degraded";
  idle_seconds: number | null;
  wake_reason: string;
  recovery_attempts: number;
};

export type SessionView = {
  session_id: string;
  state: string;
  conversation: string | null;
  human_reason: string | null;
};

export type AgentStatus = {
  account_id: string;
  runtime: string;
  uptime_seconds: number;
  running: boolean;
  paused: boolean;
  pause_reason: string;
  current_task_id: string | null;
  current_screen: string | null;
  last_action: string | null;
  action_count: number | null;
  auth_state: string | null;
  security_challenge: boolean | null;
  sessions: SessionView[];
};

export type HealthCheck = { ok: boolean; detail: string };
export type Health = { account_id: string; checks: Record<string, HealthCheck> };

export type Task = {
  id: string;
  capability: string;
  platform: string;
  priority: number;
  status: string;
  error: string | null;
  send_status: string | null;
  send_attempt_count: number | null;
  created_at: string;
};

export type ActivityRow = {
  id: number;
  task_id: string;
  action_name: string;
  success: number | null;
  error: string | null;
  created_at: string;
  capability: string | null;
};

export type Escalation = {
  id: number;
  conversation_id: string;
  customer_id: string;
  reason: string;
  status: string;
  created_at: string;
};

export type ConversationControl = {
  mode: "AUTO" | "WAITING_HUMAN" | "HUMAN";
  operator_id: string | null;
  takeover_source: "MANUAL" | "ESCALATION" | null;
  reason: string | null;
};

export type Conversation = {
  conversation_id: string;
  last_message_at: string;
  message_count: number;
  inbound: number;
  outbound: number;
  username: string | null;
  display_name: string | null;
  avatar_url: string | null;
  customer_id: string | null;
  intent: string | null;
  stage: string | null;
  requires_human: boolean | null;
  summary: string | null;
  control: ConversationControl;
};

// Inbound (from the customer): `src` is the real CDN URL the browser itself
// rendered, embeddable as-is. Outbound (from the shop/agent): always a
// product-catalogue file, so `path` is an absolute filesystem path served
// through `conversationMediaUrl` below rather than used directly.
export type MessageMedia = {
  kind: "image" | "video";
  src?: string;
  path?: string;
  name?: string;
  width?: number;
  height?: number;
};

// Bản dịch tiếng Việt hiện dưới bong bóng tin khi shop không trả lời bằng
// tiếng Việt (dashboard_api/routes/translations.py). Chỉ để nhân viên đọc -
// không bao giờ được gửi cho khách. `enabled` false: shop tiếng Việt, không
// có gì để dịch. `remaining` > 0: còn tin chưa dịch, hỏi lại để dịch tiếp.
export type ConversationTranslations = {
  enabled: boolean;
  translations: { message_id: string; direction: string; text: string }[];
  remaining: number;
  error: string | null;
};

export type ConversationDetail = {
  conversation_id: string;
  profile: Record<string, unknown> | null;
  facts: { fact_type: string; fact_value: string; confidence: number | null }[];
  state: Record<string, unknown>;
  messages: {
    message_id: string;
    direction: string;
    text: string;
    created_at: string;
    // "unverified": an agent reply the thread showed as its newest bubble
    // but could not place after the customer's message - very likely
    // delivered, cleared to an ordinary source by the next thread read.
    source: string;
    media: MessageMedia[];
    // Client-only, never from the backend. `pending` marks Customers.tsx's
    // optimistic bubble while a just-submitted staff message waits on its
    // real, DB-backed copy; `failed` marks one the agent could not verify as
    // sent, which has no database row to come back as. `uncertain` is a failed
    // send the agent may still have delivered (Send was pressed but the
    // thread did not prove it) - "check TikTok", not "did not arrive".
    pending?: boolean;
    failed?: boolean;
    uncertain?: boolean;
  }[];
  control: ConversationControl;
};

/** One workspace's global-search hits (⌘K). Grouped and capped server-side
 * - see dashboard_api/main.py's search_account. */
export type SearchResults = {
  customers: {
    conversation_id: string;
    display_name: string | null;
    username: string | null;
    customer_id: string | null;
    phone: string | null;
    last_message_at: string | null;
    /** Why it matched: who they are, or something they/we said. */
    matched: "identity" | "message";
    snippet: string | null;
  }[];
  orders: {
    id: number;
    customer_name: string;
    status: string;
    total_amount: number | null;
    items: string;
    created_at: string | null;
  }[];
  products: {
    id: string;
    name: string;
    price: number | null;
    stock: number | null;
    is_active: boolean;
  }[];
};

export type Notification = {
  id: number;
  account_id: string;
  kind: string;
  severity: string;
  title: string;
  detail: string | null;
  created_at: string;
  // Already present on the wire (NotificationStore.list() parses it) - just
  // not declared until Alerts needed it to deep-link into a conversation.
  payload?: Record<string, unknown>;
};

export type MediaFile = {
  type?: "image" | "video";
  name?: string;
  relative_path?: string;
  path?: string;
  size?: number;
  exists?: boolean;
  valid_type?: boolean;
};

export type Product = {
  id: string;
  name: string;
  brand?: string;
  category?: string;
  form?: string;
  package?: string;
  volume?: string;
  description?: string;
  ingredients?: string[];
  benefits?: string[];
  warnings?: string[];
  keywords?: string[];
  tags?: string[];
  price?: number | null;
  /** For a combo this is derived from its components - how many combos can
   * still be assembled - not a number anyone typed in. */
  stock?: number | null;
  item_type?: ItemType;
  cost_price?: number | null;
  currency?: string;
  shelf_life_days?: number | null;
  is_active?: boolean;
  low_stock_threshold?: number | null;
  combo_items?: ComboItem[];
  media: { images: MediaFile[]; videos: MediaFile[] };
};

export type ItemType = "single" | "combo";

export type ComboItem = {
  product_id: string;
  quantity: number;
};

/** Editable fields for create/update - everything but id and media, which
 * have their own dedicated endpoints. */
export type ProductInput = {
  name: string;
  brand?: string;
  category?: string;
  form?: string;
  package?: string;
  volume?: string;
  description?: string;
  ingredients?: string[];
  benefits?: string[];
  warnings?: string[];
  keywords?: string[];
  tags?: string[];
  price?: number | null;
  stock?: number | null;
  item_type?: ItemType;
  cost_price?: number | null;
  currency?: string;
  shelf_life_days?: number | null;
  is_active?: boolean;
  low_stock_threshold?: number | null;
  combo_items?: ComboItem[];
};

/** A file uploaded for a chat message, before it is sent. */
export type ChatAttachment = { id: string; kind: "image" | "video"; name: string; size: number };

export type OrderStatus = "pending" | "packing" | "shipping" | "done" | "returned" | "cancelled";

/** One product on an order, as it was when the order was taken: the server
 * fills `name` and `unit_price` from the shop's catalogue. */
export type OrderLineItem = {
  product_id: string;
  name: string;
  quantity: number;
  unit_price: number | null;
};

export type Order = {
  id: number;
  account_id: string;
  customer_name: string;
  conversation_id: string | null;
  /** Human-readable line ("2x Milo, 1x Sữa Ông Thọ"), built by the server from
   * `line_items`; typed by hand on orders logged before the product picker. */
  items: string;
  line_items: OrderLineItem[];
  /** The order took its products out of stock when placed; false for orders
   * logged before orders touched stock, which never move it. */
  stock_deducted: boolean;
  total_amount: number | null;
  status: OrderStatus;
  shipping_address: string;
  note: string;
  created_at: string;
  updated_at: string;
};

/** Editable fields for create/update - operator-entered, not synced from
 * TikTok Shop (the agent has no real order-reading capability yet). */
export type OrderInput = {
  customer_name: string;
  conversation_id?: string | null;
  /** Only for an order with no picked products (see Order.items). */
  items?: string;
  /** Picked products; the server looks up each name and price itself. */
  line_items?: { product_id: string; quantity: number }[];
  total_amount?: number | null;
  status?: OrderStatus;
  shipping_address?: string;
  note?: string;
};

export type RunStep = {
  account_id: string;
  capability: string;
  status: string;
  task_id: string | null;
  error: string | null;
};

export type RunPlan = {
  id: string;
  mode: string;
  status: string;
  created_at: string;
  finished_at: string | null;
  steps: RunStep[];
};

/** The "Nhịp quét & trả lời" knobs as the workspace's config file has them,
 *  and the range the server will accept for each. */
export type PacingSettings = {
  values: Record<string, number | null>;
  limits: Record<string, { min: number; max: number }>;
};

export type SettingsResult = {
  applied: Record<string, unknown>;
  restart_required: Record<string, string>;
  rejected: Record<string, string>;
};

export type ScenarioExample = { customer: string; agent: string };

export type ReplyLanguageOption = { code: string; label: string };

// Dashboard > Kịch bản: kịch bản mẫu, giọng văn và ngôn ngữ trả lời. Danh sách
// ngôn ngữ do backend trả về (agent/reply_language.py) nên FE không tự liệt kê.
export type ScenarioSettings = {
  examples: ScenarioExample[];
  reply_style: string[];
  // True when no rules were saved for this workspace: reply_style is then the
  // reply language's default set, and changes with the language.
  reply_style_is_default: boolean;
  reply_language: string;
  reply_languages: ReplyLanguageOption[];
};

// The product a customer question naming none is about
// (products.default_product_id, agent/product_lookup.py). `default_product_id`
// is the shop's choice (null = automatic); `effective_default_product_id` is
// what the agent actually uses - null when it will ask the customer instead.
export type DefaultProduct = {
  default_product_id: string | null;
  effective_default_product_id: string | null;
};

// Dashboard > Kịch bản > Giao hàng (products.shipping, agent/shop_policy.py).
// Every field optional: a blank one is simply not stated to customers.
export type ShippingSettings = {
  fee?: number;
  free_from_quantity?: number;
  delivery_days?: string;
  cash_on_delivery?: boolean;
  area?: string;
};

export type FollowUpRule = {
  id: string;
  delay_minutes: number;
  message: string;
  enabled: boolean;
};

export type Summary = {
  tasks: number;
  tasks_pending: number;
  tasks_running: number;
  tasks_failed: number;
  tasks_completed: number;
  customers: number;
  messages: number;
  escalations_open: number;
};

export type LogFileName = "runtime" | "chrome" | "xvfb" | "x11vnc" | "novnc";
export type LogLevel = "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL";

/** One logging record: the timestamped line plus any traceback under it.
 *  level is null for lines written outside `logging` (startup warnings). */
export type LogEntry = { level: LogLevel | null; text: string };

/** GET /api/logs/{account_id} - admin only. `truncated` means the scan
 *  stopped before the start of the file, so older matches may exist. */
export type WorkspaceLog = {
  file: LogFileName;
  files: { name: LogFileName; size: number; modified: number }[];
  entries: LogEntry[];
  size: number;
  truncated: boolean;
};

export type AgentEvent = {
  account_id: string;
  type: string;
  ts: string;
  payload: Record<string, unknown>;
};

/** Workspace status as the sidebar shows it. Derived, never stored. */
export type WorkspaceState =
  | "running"
  | "paused"
  | "captcha"
  | "starting"
  | "error"
  | "idle"
  // Chrome is up but the agent process is not. Legitimate right after a
  // workspace is created (Chrome opens for the first TikTok login before an
  // API key exists) and the tell-tale of an agent that crashed at boot -
  // most often a missing GEMINI_API_KEY. Reported distinctly because the
  // controls were keyed on the agent alone: this read as "idle", offered
  // Start instead of Stop, and left Chrome running with no way to close it.
  | "chrome_only"
  | "sleeping"
  | "waking"
  | "degraded";

export const WORKSPACE_STATE_LABEL: Record<WorkspaceState, string> = {
  running: "Running",
  paused: "Paused",
  captcha: "Cần người xử lý",
  starting: "Đang khởi động",
  error: "Mất kết nối",
  idle: "Idle",
  chrome_only: "Chrome đang mở, agent đã tắt",
  sleeping: "Đang ngủ (tiết kiệm RAM)",
  waking: "Đang đánh thức",
  degraded: "Không tự đánh thức được - cần thao tác tay",
};

export function workspaceState(
  account: Account,
  status: AgentStatus | undefined,
): WorkspaceState {
  // Sleep/wake only ever stops Chrome, never the agent process, so
  // account.processes.agent.running stays true throughout - checked first,
  // ahead of the agent-process-based reasoning below.
  if (account.lifecycle_state === "degraded") return "degraded";
  if (account.lifecycle_state === "sleeping") return "sleeping";
  if (account.lifecycle_state === "waking" || account.lifecycle_state === "recovery") return "waking";
  if (!account.processes.agent.running) {
    return account.processes.chrome.running ? "chrome_only" : "idle";
  }
  // The process is up but the control port is not answering yet or any more:
  // starting is the benign reading, error is the one that needs attention.
  if (!account.control_connected) return status ? "error" : "starting";
  if (status?.security_challenge) return "captcha";
  if (status?.sessions.some((s) => s.human_reason)) return "captcha";
  if (status?.paused) return "paused";
  return "running";
}

/**
 * What a failed call looks like to the rest of the app.
 *
 * `message` is written for the person reading it: a sentence, in their
 * language, with no HTTP code and no JSON in it. Before this the raw
 * response was thrown as-is, so the screen showed things like
 * `401: {"detail":"sai tài khoản hoặc mật khẩu"}` and a validation error cut
 * off mid-JSON at 200 characters. `status` and `detail` stay available for
 * code that needs them (the auth gate keys on 401; a toast can show the
 * technical detail on its second line).
 */
export class ApiError extends Error {
  readonly status: number;
  /** The server's own words, when it gave any - already human-readable
   * for this API's HTTPException messages. */
  readonly detail: string | null;

  constructor(status: number, message: string, detail: string | null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

// FastAPI's validation failures arrive as a list of {loc, msg}; everything
// else this API raises is `{"detail": "<sentence>"}`.
function detailFromBody(body: string): string | null {
  try {
    const parsed = JSON.parse(body);
    const detail = parsed?.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((item) => {
          const field = Array.isArray(item?.loc) ? item.loc.filter((p: unknown) => p !== "body").join(".") : "";
          return field ? `${field}: ${item?.msg ?? ""}` : String(item?.msg ?? "");
        })
        .filter(Boolean)
        .join("; ");
    }
  } catch {
    // not JSON - fall through
  }
  const text = body.trim();
  return text && !text.startsWith("<") ? text.slice(0, 300) : null;
}

const STATUS_MESSAGE: Record<number, string> = {
  401: "Phiên đăng nhập đã hết. Vui lòng đăng nhập lại.",
  403: "Bạn không có quyền thực hiện thao tác này.",
  404: "Không tìm thấy dữ liệu được yêu cầu.",
  409: "Thao tác xung đột với trạng thái hiện tại. Tải lại rồi thử lại.",
  429: "Bạn thao tác quá nhanh. Đợi một lát rồi thử lại.",
};

// The auth middleware's one generic 401 body. Any *other* 401 detail is a
// specific message from a specific endpoint - the login route's "sai tài khoản
// hoặc mật khẩu" above all - and must reach the screen as written.
const MIDDLEWARE_401 = "not authenticated";

export function messageFor(status: number, detail: string | null): string {
  // The server's own sentence beats any generic wording - it is specific
  // ("combo cần ít nhất một sản phẩm lẻ") and already in the user's language.
  // The one exception is the middleware's "not authenticated", which is a
  // code path, not advice.
  if (detail && !(status === 401 && detail === MIDDLEWARE_401)) return detail;
  if (STATUS_MESSAGE[status]) return STATUS_MESSAGE[status];
  if (status >= 500) return "Máy chủ gặp lỗi. Thử lại sau ít phút; nếu vẫn lỗi, kiểm tra log supervisor.";
  return "Yêu cầu không thực hiện được.";
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      ...init,
    });
  } catch (cause) {
    // fetch itself rejecting means the request never got an answer: the
    // supervisor is down or restarting, or the network is. Status 0 is the
    // browser's own convention for that.
    throw new ApiError(0, "Không kết nối được tới máy chủ. Kiểm tra dịch vụ đang chạy hoặc kết nối mạng.", null);
  }
  if (!response.ok) {
    const detail = detailFromBody(await response.text());
    throw new ApiError(response.status, messageFor(response.status, detail), detail);
  }
  return response.json() as Promise<T>;
}

const post = (path: string, body?: unknown) =>
  request<any>(path, { method: "POST", body: body ? JSON.stringify(body) : undefined });

// Each workspace has its own dedicated noVNC instance (own Xvfb, own x11vnc,
// own websockify - dashboard_api/processes.py) rather than every workspace
// sharing one screen.
//
// The page is reached through the dashboard's own origin at /vnc/<id>/, not
// at that instance's port: websockify binds 127.0.0.1 and nothing else, so
// the port is unreachable from anywhere but the server itself. nginx makes
// that hop after asking the supervisor whether this user may see this
// workspace (see vncUrl below, and dashboard/nginx.conf.template). Nothing
// downstream - an SSH tunnel, a Cloudflare hostname - has to carry the VNC
// port range, and it must not be made to.
//
// autoconnect/reconnect are noVNC's own query params (see
// /usr/share/novnc/app/ui.js's UI.initSetting calls) - safe to rely on since
// this is the stock noVNC page apt-get installs, not something this repo
// maintains. autoconnect skips the "Connect" landing screen (x11vnc runs
// with -nopw - unchanged from before, see processes.py's _x11vnc_command -
// so there is no password prompt to skip past either); reconnect=true
// retries on its own if the websocket drops instead of leaving the operator
// stuck back on the landing screen.
//
// Deliberately NOT resize=scale: confirmed live that it shrinks the whole
// desktop into a tall, narrow sliver instead of scaling it to fit - noVNC
// only recomputes the scale (UI.applyResizeMode/updateViewClip) on a
// *change* to the resize setting (ui.js's addSettingChangeHandler), not once
// the RFB session actually reports the real framebuffer size, so starting
// already-scaled via autoconnect races that and freezes on a wrong
// computation. The operator can still turn scaling on by hand from noVNC's
// own settings gear if a tab is too small for the full desktop.
function vncUrl(account: { id: string }): string {
  // Same origin as the dashboard, and relative on purpose. The old form was
  // `http://<hostname>:<novnc_port>/vnc.html`, which only ever worked when
  // the dashboard was itself on loopback: websockify binds 127.0.0.1 and
  // nothing else, so reaching the dashboard over a public IP or a tailnet
  // built a URL to a port that refuses every connection. Going through
  // /vnc/<id>/ means nginx does the loopback hop on the server, after the
  // supervisor has checked that this user may see this workspace.
  //
  // `path` is not optional: noVNC builds its WebSocket URL as host + "/" +
  // path, anchored at the root rather than relative to the page - so without
  // it the page loads and then hangs on a black screen trying /websockify.
  const base = `/vnc/${encodeURIComponent(account.id)}/`;
  return `${base}vnc.html?autoconnect=true&reconnect=true&path=${base.slice(1)}websockify`;
}

/** Ensures a workspace's own Chrome/display stack is up (waking it from
 * sleep if needed - see the backend's focus-window handler), then opens (or
 * refocuses) that workspace's own noVNC tab. Shared by AlertsPage and
 * HomePage so both "Xem Chrome (VNC)" buttons behave identically. */
export function openVncWindow(account: { id: string }): void {
  // Named window per account (not "_blank", not one shared name): each
  // workspace has its own noVNC page now, so reusing one window name across
  // workspaces would silently replace an open tab's URL out from under
  // whichever workspace it was previously showing. Deliberately WITHOUT
  // "noopener": noopener puts the new tab in its own unrelated browsing
  // context group, which breaks browser-side name-based window reuse - every
  // later window.open with the same name would then open yet another tab
  // instead of finding this one (confirmed live: 3 clicks -> 3 tabs). Safe to
  // drop noopener here since this is our own same-origin noVNC page, not an
  // arbitrary/untrusted URL.
  window.open(vncUrl(account), `tiktok-agent-vnc-${account.id}`);
}

export type EgwRoute = {
  id: string;
  source_ip: string;
  status: string;
  enabled: boolean;
  last_error: string | null;
  // Workspace ids currently mapped onto this route. This project runs one
  // shop per IP, so anything non-empty means "taken" unless it is this
  // very workspace.
  held_by: string[];
};

export type EgwRoutesResponse = {
  // false: this machine has no egw wired up - hide the feature entirely.
  configured: boolean;
  routes: EgwRoute[];
};

export const api = {
  system: () => request<{ chrome_available: boolean; chrome_binary: string | null }>("/api/system"),
  accounts: () => request<Account[]>("/api/accounts"),
  createAccount: (
    label: string,
    profileDirName = "",
    apiKey = "",
  ) =>
    // account_id is intentionally omitted - the backend generates a random
    // one (dashboard_api/accounts.py's AccountRegistry._generate_account_id).
    post("/api/accounts", {
      account_id: "", label,
      profile_dir_name: profileDirName,
      api_key: apiKey,
    }),
  deleteAccount: (id: string, purge = false) =>
    request(`/api/accounts/${id}${purge ? "?purge=true" : ""}`, { method: "DELETE" }),
  // Edit after creation - e.g. filling in the API key later. Omitted fields
  // (undefined) are left unchanged server-side; api_key: "" explicitly clears it.
  updateAccount: (id: string, fields: { label?: string; api_key?: string; egw_token?: string }) =>
    request<Account>(`/api/accounts/${id}`, {
      method: "PATCH",
      body: JSON.stringify(fields),
    }),

  // The one Google Sheet every workspace exports to (GSHEET_URL in .env);
  // url is "" when none is configured.
  sheetLink: () => request<{ url: string }>("/api/sheet-link"),

  egwRoutes: () => request<EgwRoutesResponse>("/api/egw/routes"),
  // Maps the workspace onto `route` on egw and stores the issued token.
  // Takes effect at the workspace's next Start (restart_required says
  // whether one is running right now).
  enableEgw: (id: string, route: string) =>
    request<Account & { restart_required: boolean }>(`/api/accounts/${id}/egw`, {
      method: "POST",
      body: JSON.stringify({ route }),
    }),
  disableEgw: (id: string) =>
    request<Account & { restart_required: boolean }>(`/api/accounts/${id}/egw`, { method: "DELETE" }),

  start: (id: string, runtime: string, with_chrome: boolean) =>
    post(`/api/accounts/${id}/start`, { runtime, with_chrome }),
  stop: (id: string, include_chrome = true) =>
    post(`/api/accounts/${id}/stop`, { include_chrome }),
  pause: (id: string, reason: string) => post(`/api/accounts/${id}/pause`, { reason }),
  resume: (id: string) => post(`/api/accounts/${id}/resume`),
  submitTask: (id: string, capability: string, payload: Record<string, unknown>) =>
    post(`/api/accounts/${id}/tasks`, { capability, payload }),

  // Sleep/wake: heartbeat while the Agent page is open (blocks auto-sleep,
  // and wakes a sleeping workspace as a side effect), pin to block it
  // indefinitely, manual wake for an immediate retry (e.g. before focusing
  // the VNC window).
  heartbeat: (id: string) => post(`/api/accounts/${id}/heartbeat`),
  setPinned: (id: string, pinned: boolean) => post(`/api/accounts/${id}/pin`, { pinned }),
  wake: (id: string) => post(`/api/accounts/${id}/wake`),

  status: (id: string) => request<AgentStatus>(`/api/accounts/${id}/status`),
  health: (id: string) => request<Health>(`/api/accounts/${id}/health`),
  summary: (id: string) => request<Summary>(`/api/accounts/${id}/summary`),
  tasks: (id: string, limit = 50) => request<Task[]>(`/api/accounts/${id}/tasks?limit=${limit}`),
  activity: (id: string, limit = 60) =>
    request<ActivityRow[]>(`/api/accounts/${id}/activity?limit=${limit}`),
  escalations: (id: string) => request<Escalation[]>(`/api/accounts/${id}/escalations`),
  search: (id: string, q: string, signal?: AbortSignal) =>
    request<SearchResults>(`/api/accounts/${id}/search?q=${encodeURIComponent(q)}`, { signal }),
  conversations: (id: string) =>
    request<Conversation[]>(`/api/accounts/${id}/conversations`),
  conversation: (id: string, conversationId: string) =>
    request<ConversationDetail>(
      `/api/accounts/${id}/conversations/${encodeURIComponent(conversationId)}`,
    ),
  conversationTranslations: (id: string, conversationId: string) =>
    request<ConversationTranslations>(
      `/api/accounts/${id}/conversations/${encodeURIComponent(conversationId)}/translations`,
      { method: "POST" },
    ),
  takeoverConversation: (id: string, conversationId: string, operatorId = "local_operator") =>
    post(
      `/api/accounts/${id}/conversations/${encodeURIComponent(conversationId)}/takeover`,
      { operator_id: operatorId },
    ),
  releaseConversation: (id: string, conversationId: string) =>
    post(
      `/api/accounts/${id}/conversations/${encodeURIComponent(conversationId)}/release`,
    ),
  sendConversationMessage: (
    id: string,
    conversationId: string,
    text: string,
    clientMessageId: string,
    attachments: string[] = [],
    operatorId = "local_operator",
  ) =>
    post(
      `/api/accounts/${id}/conversations/${encodeURIComponent(conversationId)}/messages`,
      { text, client_message_id: clientMessageId, operator_id: operatorId, attachments },
    ),
  /** Upload a file an operator attaches in the chat; send it by the returned id. */
  uploadChatAttachment: async (accountId: string, file: File): Promise<ChatAttachment> => {
    const body = new FormData();
    body.append("file", file);
    const response = await fetch(`/api/accounts/${accountId}/chat-attachments`, { method: "POST", body });
    if (!response.ok) {
      let detail = "";
      try { detail = String((await response.json()).detail ?? ""); } catch { /* not JSON */ }
      throw new Error(detail || `Không tải được tệp lên (${response.status})`);
    }
    return response.json();
  },
  readSettings: (id: string) => request<PacingSettings>(`/api/accounts/${id}/settings`),
  settings: (id: string, values: Record<string, unknown>) =>
    request<SettingsResult>(`/api/accounts/${id}/settings`, {
      method: "PATCH",
      body: JSON.stringify({ values }),
    }),
  scenario: (id: string) => request<ScenarioSettings>(`/api/accounts/${id}/scenario`),
  saveScenario: (id: string, examples: ScenarioExample[]) =>
    request<{ examples: ScenarioExample[] }>(`/api/accounts/${id}/scenario`, {
      method: "PUT",
      body: JSON.stringify({ examples }),
    }),
  saveReplyStyle: (id: string, rules: string[]) =>
    request<{ reply_style: string[]; reply_style_is_default: boolean }>(`/api/accounts/${id}/reply-style`, {
      method: "PUT",
      body: JSON.stringify({ rules }),
    }),
  shipping: (id: string) =>
    request<{ shipping: ShippingSettings | null }>(`/api/accounts/${id}/shipping`),
  saveShipping: (id: string, shipping: ShippingSettings | null) =>
    request<{ shipping: ShippingSettings | null }>(`/api/accounts/${id}/shipping`, {
      method: "PUT",
      body: JSON.stringify({ shipping }),
    }),
  saveReplyLanguage: (id: string, language: string) =>
    request<{ reply_language: string; reply_style: string[]; reply_style_is_default: boolean }>(
      `/api/accounts/${id}/reply-language`, {
      method: "PUT",
      body: JSON.stringify({ language }),
    }),
  followups: (id: string) =>
    request<{ rules: FollowUpRule[] }>(`/api/accounts/${id}/followups`),
  saveFollowups: (id: string, rules: (Omit<FollowUpRule, "id"> & { id?: string })[]) =>
    request<{ rules: FollowUpRule[] }>(`/api/accounts/${id}/followups`, {
      method: "PUT",
      body: JSON.stringify({ rules }),
    }),

  notifications: () => request<Notification[]>("/api/notifications"),
  resolveNotification: (notificationId: number) =>
    post(`/api/notifications/${notificationId}/resolve`),
  focusWorkspaceWindow: (accountId: string) =>
    post(`/api/accounts/${accountId}/focus-window`),

  products: (accountId: string) =>
    request<{
      products: Product[];
      missing_media: { product_id: string; type: string; path?: string }[];
      invalid_product_directories: { directory: string; reason: string }[];
    } & DefaultProduct>(`/api/accounts/${accountId}/products`),
  saveDefaultProduct: (accountId: string, productId: string | null) =>
    request<DefaultProduct>(`/api/accounts/${accountId}/default-product`, {
      method: "PUT",
      body: JSON.stringify({ product_id: productId }),
    }),
  createProduct: (accountId: string, productId: string, fields: ProductInput) =>
    post(`/api/accounts/${accountId}/products`, { product_id: productId, fields }),
  updateProduct: (accountId: string, productId: string, fields: ProductInput) =>
    request<Product>(`/api/accounts/${accountId}/products/${encodeURIComponent(productId)}`, {
      method: "PUT",
      body: JSON.stringify(fields),
    }),
  deleteProduct: (accountId: string, productId: string) =>
    request(`/api/accounts/${accountId}/products/${encodeURIComponent(productId)}`, {
      method: "DELETE",
    }),
  uploadProductMedia: async (
    accountId: string,
    productId: string,
    mediaType: "image" | "video",
    file: File,
  ): Promise<Product> => {
    const body = new FormData();
    body.append("media_type", mediaType);
    body.append("file", file);
    const response = await fetch(
      `/api/accounts/${accountId}/products/${encodeURIComponent(productId)}/media`,
      { method: "POST", body },
    );
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(`${response.status}: ${detail.slice(0, 200)}`);
    }
    return response.json();
  },
  deleteProductMedia: (accountId: string, productId: string, relativePath: string) =>
    request<Product>(
      `/api/accounts/${accountId}/products/${encodeURIComponent(productId)}/media`,
      { method: "DELETE", body: JSON.stringify({ relative_path: relativePath }) },
    ),
  productMediaUrl: (accountId: string, productId: string, relativePath: string) =>
    `/api/accounts/${accountId}/products/${encodeURIComponent(productId)}/media/${relativePath}`,
  conversationMediaUrl: (accountId: string, path: string) =>
    `/api/accounts/${accountId}/conversation-media?path=${encodeURIComponent(path)}`,

  orders: (accountId: string) => request<Order[]>(`/api/accounts/${accountId}/orders`),
  createOrder: (accountId: string, fields: OrderInput) =>
    post(`/api/accounts/${accountId}/orders`, fields),
  updateOrder: (accountId: string, orderId: number, fields: OrderInput) =>
    request<Order>(`/api/accounts/${accountId}/orders/${orderId}`, {
      method: "PUT",
      body: JSON.stringify(fields),
    }),
  deleteOrder: (accountId: string, orderId: number) =>
    request(`/api/accounts/${accountId}/orders/${orderId}`, { method: "DELETE" }),
  setOrderStatus: (accountId: string, orderId: number, status: OrderStatus) =>
    request<Order>(`/api/accounts/${accountId}/orders/${orderId}/status`, {
      method: "POST",
      body: JSON.stringify({ status }),
    }),

  /** Registration does not sign anyone in: the account is created inactive
   *  and an admin has to approve it, so the answer is a receipt, not a user.
   *  The same receipt comes back when the address is already taken - that is
   *  deliberate, it stops the endpoint being used to test whether an address
   *  has an account here. */
  register: (email: string, password: string) =>
    post("/api/auth/register", { email, password }) as Promise<RegistrationReceipt>,
  login: (email: string, password: string) =>
    post("/api/auth/login", { email, password }) as Promise<User>,
  logout: () => post("/api/auth/logout"),
  me: () => request<User>("/api/auth/me"),
  /** Web Push (lib/webPush.ts, dashboard_api/web_push.py). */
  pushConfig: () => request<{ public_key: string }>("/api/push/config"),
  pushSubscribe: (subscription: PushSubscriptionJSON) =>
    post("/api/push/subscriptions", subscription),
  pushUnsubscribe: (endpoint: string) => post("/api/push/subscriptions/delete", { endpoint }),
  pushTest: () => post("/api/push/test") as Promise<{ delivered: number }>,
  forgotPassword: (email: string) => post("/api/auth/forgot-password", { email }),
  resetPassword: (token: string, password: string) =>
    post("/api/auth/reset-password", { token, password }),
  /** Signed-in user changing their own password. Signs out every other
   *  device; this browser gets a fresh session cookie and stays in. */
  changePassword: (currentPassword: string, newPassword: string) =>
    post("/api/auth/change-password", {
      current_password: currentPassword, new_password: newPassword,
    }),

  users: () => request<ManagedUser[]>("/api/users"),
  // Admin-only, unrelated to the public self-registration form below - see
  // dashboard_api/main.py's create_user_as_admin. Always creates a plain
  // (non-admin) user.
  createUser: (email: string, password: string) =>
    post("/api/users", { email, password }) as Promise<ManagedUser>,
  grantAccess: (userId: number, accountId: string) =>
    post(`/api/users/${userId}/access`, { account_id: accountId }),
  revokeAccess: (userId: number, accountId: string) =>
    request(`/api/users/${userId}/access/${encodeURIComponent(accountId)}`, { method: "DELETE" }),
  deactivateUser: (userId: number) => post(`/api/users/${userId}/deactivate`),
  activateUser: (userId: number) => post(`/api/users/${userId}/activate`),
  deleteUser: (userId: number) =>
    request(`/api/users/${userId}`, { method: "DELETE" }),
  sendUserResetLink: (userId: number) => post(`/api/users/${userId}/reset-password-link`),
  setUserPassword: (userId: number, password: string) =>
    post(`/api/users/${userId}/set-password`, { password }),
  /** null clears the quota back to "chưa cấp", which the backend reads as 0. */
  setUserMaxWorkspaces: (userId: number, maxWorkspaces: number | null) =>
    request(`/api/users/${userId}/max-workspaces`, {
      method: "PATCH", body: JSON.stringify({ max_workspaces: maxWorkspaces }),
    }),

  workspaceLog: (
    accountId: string,
    params: { file: LogFileName; lines: number; level: LogLevel | null; q: string },
  ) => {
    const query = new URLSearchParams({ file: params.file, lines: String(params.lines) });
    if (params.level) query.set("level", params.level);
    if (params.q) query.set("q", params.q);
    return request<WorkspaceLog>(`/api/logs/${encodeURIComponent(accountId)}?${query}`);
  },

  runs: () => request<RunPlan[]>("/api/runs"),
  startRun: (mode: string, steps: { account_id: string; capability: string }[]) =>
    post("/api/runs", { mode, steps }),
  cancelRun: (planId: string) => post(`/api/runs/${planId}/cancel`),
};

(() => {
  "use strict";

  // TikTok Web profile. Selector lists are ordered from the most semantic and
  // stable option to the weakest one. Generated CSS class names are never used
  // as a primary strategy because TikTok rotates the hash on every build; the
  // few `[class*="..."]` entries below only match the stable styled-component
  // suffix and are always the last resort inside an already-located container.
  //
  // The Messages inbox on tiktok.com is normally an overlay panel drawn on top
  // of the feed, not a full page. Everything here is written for that: the
  // panel is located first, and conversation rows are only ever queried inside
  // it, so sidebar navigation can never be mistaken for a conversation.
  const SELECTORS = {
    messagesNav: [
      '[data-e2e="nav-messages"]',
      '[data-e2e="nav-message"]',
      '[data-e2e="top-dm-icon"]',
      'a[href="/messages"]',
      'a[href^="/messages"]',
      '[role="link"][aria-label*="message" i]',
      '[role="button"][aria-label*="message" i]',
      '[role="link"][aria-label*="tin nhắn" i]',
      '[role="button"][aria-label*="tin nhắn" i]'
    ],
    activityNav: [
      '[data-e2e="inbox-icon"]',
      '[data-e2e="nav-inbox"]',
      '[data-e2e="nav-notification"]',
      '[role="button"][aria-label*="notification" i]',
      '[role="link"][aria-label*="notification" i]',
      '[role="button"][aria-label*="activity" i]',
      '[role="button"][aria-label*="thông báo" i]'
    ],
    // The inbox drawer / Messages page container. Conversation rows are only
    // read from inside one of these.
    messagesPanel: [
      '[data-e2e="chat-list"]',
      '[data-e2e="dm-new-conversation-list"]',
      '[data-e2e*="conversation-list" i]',
      '[data-e2e*="chat-list" i]',
      '[data-e2e*="inbox-panel" i]',
      '[role="list"][aria-label*="conversation" i]',
      '[role="list"][aria-label*="message" i]',
      '[role="list"][aria-label*="tin nhắn" i]',
      '[class*="DivChatListContainer" i]',
      '[class*="DivChatListWrapper" i]',
      '[class*="DivInboxContainer" i]',
      '[class*="DivChatBoxContainer" i]'
    ],
    conversationItem: [
      '[data-e2e="chat-list-item"]',
      '[data-e2e="dm-new-conversation-item"]',
      '[data-e2e*="conversation-item" i]',
      '[data-conversation-id]',
      '[class*="DivItemWrapper" i]',
      '[role="listitem"]'
    ],
    conversationName: [
      '[data-e2e="chat-list-nickname"]',
      '[data-e2e="dm-new-conversation-nickname"]',
      '[data-e2e*="nickname" i]',
      '[data-e2e*="uniqueid" i]',
      '[class*="PInfoNickname" i]',
      '[class*="Nickname" i]'
    ],
    conversationPreview: [
      '[data-e2e="chat-list-msg"]',
      '[data-e2e*="preview" i]',
      '[data-e2e*="last-message" i]',
      '[class*="PInfoExtractTime" i]',
      '[class*="Preview" i]'
    ],
    conversationHeader: [
      '[data-e2e="chat-nickname"]',
      '[data-e2e="dm-new-chat-nickname"]',
      '[data-e2e="chat-uniqueid"]',
      '[data-e2e*="chat-title" i]',
      '[class*="ChatHeader" i] [class*="Nickname" i]'
    ],
    // The avatar image next to the open conversation's nickname header - not
    // the row list, only the currently active thread (that's all the CRM
    // profile capture in perception/browser_observer.py reads from).
    conversationHeaderAvatar: [
      '[data-e2e="chat-avatar"] img',
      '[data-e2e*="chat-avatar" i] img',
      '[class*="ChatHeader" i] img[class*="Avatar" i]',
      '[class*="ChatHeader" i] img'
    ],
    chatBox: [
      '[data-e2e="dm-new-chatbox"]',
      '[data-e2e="chat-box"]',
      '[data-e2e*="chatbox" i]',
      '[class*="DivChatBox" i]'
    ],
    messageList: [
      '[data-e2e="chat-message-list"]',
      '[data-e2e*="message-list" i]',
      '[role="log"]'
    ],
    messageItem: [
      '[data-message-id]',
      '[data-e2e="chat-item"]',
      '[data-e2e*="message-item" i]',
      '[data-e2e="dm-new-message-text"]'
    ],
    messageText: [
      '[data-e2e="chat-text"]',
      '[data-e2e="dm-new-message-text"]',
      '[data-e2e*="message-text" i]'
    ],
    messageInput: [
      '[data-e2e="message-input-area"] [contenteditable="true"]',
      '[data-e2e="dm-new-input-editor"] [contenteditable="true"]',
      '[data-e2e="message-input-area"] textarea',
      '[role="textbox"][contenteditable="true"]',
      'div[contenteditable="true"][role="textbox"]',
      'textarea[placeholder]'
    ],
    sendButton: [
      '[data-e2e="message-send"]',
      '[data-e2e*="send" i]',
      'button[aria-label*="send" i]',
      'button[aria-label*="gửi" i]',
      '[role="button"][aria-label*="send" i]'
    ],
    backButton: [
      '[data-e2e="chat-back"]',
      'button[aria-label*="back" i]',
      'button[aria-label*="quay lại" i]'
    ],
    // A badge is only ever read as a descendant of a specific nav entry, so a
    // Messages badge and an Activity badge can never be summed together.
    badge: [
      '[data-e2e*="badge" i]',
      '[data-e2e*="unread" i]',
      '[class*="Badge" i]',
      '[class*="SupUnread" i]',
      'sup'
    ],
    modal: ['[role="dialog"]', '[role="alertdialog"]', '[data-e2e*="modal" i]', '[data-e2e*="popup" i]'],
    modalClose: [
      '[data-e2e*="close" i]',
      'button[aria-label*="close" i]',
      'button[aria-label*="đóng" i]',
      '[role="dialog"] button'
    ],
    login: ['input[type="password"]', 'form[action*="login" i]', '[data-e2e="login-button"]', '[data-e2e*="login" i]'],
    captcha: ['iframe[src*="captcha" i]', '[id*="captcha" i]', '[class*="captcha" i]', '[data-e2e*="captcha" i]'],
    otp: ['input[autocomplete="one-time-code"]', '[data-e2e*="verification" i]', '[data-e2e*="verify" i]']
  };

  const UNREAD_HINT = /(^|\W)(unread|chưa đọc|new message|tin nhắn mới)(\W|$)/i;
  const REQUEST_HINT = /(message request|tin nhắn chờ|yêu cầu tin nhắn|request)/i;

  // Left/top navigation entries that share the "avatar + label" shape with a
  // conversation row. A row whose whole text is one of these is navigation.
  const NAV_LABELS = new Set(
    [
      "for you", "explore", "following", "friends", "live", "upload", "profile",
      "activity", "messages", "more", "shop", "search", "log in", "sign up",
      "dành cho bạn", "khám phá", "đang follow", "bạn bè", "tải lên", "hồ sơ",
      "thông báo", "tin nhắn", "thêm", "tìm kiếm", "đăng nhập", "đăng ký"
    ].map((item) => item.toLowerCase())
  );

  // Ancestors that can never contain a conversation row.
  const NAV_CONTAINER_SELECTOR =
    'nav,[role="navigation"],[data-e2e="nav-bar"],[data-e2e*="nav-" i],header';

  const q = (list, root = document) => {
    for (const selector of list) {
      let node = null;
      try {
        node = root.querySelector(selector);
      } catch (_) {
        node = null;
      }
      if (node) return node;
    }
    return null;
  };

  const qa = (list, root = document, limit = 200) => {
    for (const selector of list) {
      let nodes = [];
      try {
        nodes = [...root.querySelectorAll(selector)];
      } catch (_) {
        nodes = [];
      }
      if (nodes.length) return nodes.slice(0, limit);
    }
    return [];
  };

  const text = (node, limit = 400) =>
    ((node && (node.innerText || node.textContent)) || "").replace(/\s+/g, " ").trim().slice(0, limit);

  const attr = (node, name) => (node && node.getAttribute && node.getAttribute(name)) || "";

  function visible(node) {
    if (!node || !node.isConnected || typeof node.getBoundingClientRect !== "function") return false;
    const rect = node.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    const style = getComputedStyle(node);
    return style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity) !== 0;
  }

  function digits(value) {
    const match = String(value || "").match(/\d+/);
    return match ? Number(match[0]) : 0;
  }

  function isNavigation(node) {
    return Boolean(node && node.closest && node.closest(NAV_CONTAINER_SELECTOR));
  }

  // ---------------------------------------------------------------------------
  // Messages panel
  // ---------------------------------------------------------------------------

  function panelFromExplicitRows() {
    // Rows TikTok marks itself already prove where the inbox is; the nearest
    // shared ancestor is the container.
    for (const selector of SELECTORS.conversationItem.slice(0, 4)) {
      let rows = [];
      try {
        rows = [...document.querySelectorAll(selector)].filter(
          (node) => visible(node) && !isNavigation(node)
        );
      } catch (_) {
        rows = [];
      }
      if (!rows.length) continue;
      let container = rows[0].parentElement;
      while (container && container !== document.body && !rows.every((row) => container.contains(row))) {
        container = container.parentElement;
      }
      if (container && container !== document.body) return container;
    }
    return null;
  }

  function findMessagesPanel() {
    for (const selector of SELECTORS.messagesPanel) {
      let nodes = [];
      try {
        nodes = [...document.querySelectorAll(selector)];
      } catch (_) {
        nodes = [];
      }
      const found = nodes.find((node) => !isNavigation(node) && visible(node));
      if (found) return found;
    }
    return panelFromExplicitRows();
  }

  function panelState() {
    const container = findMessagesPanel();
    if (!container) return { container: null, present: false, visible: false, mode: null };
    const rect = container.getBoundingClientRect();
    const isVisible = visible(container);
    // "panel" = inbox drawer overlaying the feed, "page" = full Messages route.
    // Width, not URL, decides: TikTok keeps the same URL for the drawer.
    //
    // The test is "does it fill the viewport", not "is it under 60% of the
    // viewport". The drawer's width is roughly fixed in px while innerWidth
    // is not, so the old ratio flipped a perfectly normal drawer to "page"
    // once the Chrome window got narrow enough for that fixed width to cross
    // 60% of it - and "page" is the unsafe answer: adapter.py's
    // _messages_surface_open treats "panel" as proof the inbox is open, so a
    // drawer misread as a page makes the agent believe the inbox never
    // opened and click the nav entry again. Filling the viewport is a
    // property of the real Messages route at any window size, so this stays
    // correct however small the window gets, and errs toward "panel".
    const fillsViewport = rect.width >= innerWidth - 32;
    const mode = rect.width > 0 && !fillsViewport ? "panel" : "page";
    return { container, present: true, visible: isVisible, mode, rect };
  }

  function structuralRows(panel) {
    // Fallback when TikTok ships no row-level data-e2e: the outermost visible
    // blocks inside the panel that carry an avatar and some text.
    const rows = [];
    let nodes = [];
    try {
      nodes = [...panel.querySelectorAll("li,a,div")];
    } catch (_) {
      return rows;
    }
    for (const node of nodes.slice(0, 800)) {
      if (!visible(node) || isNavigation(node)) continue;
      const avatar = node.querySelector('img,[class*="Avatar" i],[data-e2e*="avatar" i]');
      if (!avatar) continue;
      if (!text(node, 200)) continue;
      // Document order puts ancestors first, so the outermost block wins.
      if (rows.some((existing) => existing.contains(node) || node.contains(existing))) continue;
      rows.push(node);
      if (rows.length >= 60) break;
    }
    return rows;
  }

  function conversationRows() {
    const panel = panelState();
    if (!panel.container || !panel.visible) return [];
    let rows = qa(SELECTORS.conversationItem, panel.container, 60).filter(
      (node) => visible(node) && !isNavigation(node)
    );
    if (!rows.length) rows = structuralRows(panel.container);
    return rows.filter((node) => {
      const label = text(node, 120).toLowerCase();
      // A row whose entire text is a navigation label is navigation, not a chat.
      return label && !NAV_LABELS.has(label);
    });
  }

  // TikTok's own thread id, e.g. `0:1:6981093337449300994:7278178670136558593`.
  // It is not exposed as a data attribute, but the row's per-item controls
  // carry it inside their element id (`more-action-icon-0:1:...`). It is the
  // only key that survives the customer renaming themselves.
  function threadId(node) {
    const own = attr(node, "id");
    const nested = node.querySelector ? node.querySelector('[id*=":"]') : null;
    for (const value of [own, nested ? attr(nested, "id") : ""]) {
      const found = String(value || "").match(/\d+:\d+:\d+:\d+/);
      if (found) return found[0];
    }
    return "";
  }

  function conversationKey(node, index) {
    return (
      attr(node, "data-conversation-id") ||
      attr(node, "data-id") ||
      attr(node, "data-e2e-id") ||
      threadId(node) ||
      (attr(node, "href").match(/\/messages\?[^"]*u=([^&]+)/) || [])[1] ||
      conversationUsername(node) ||
      `row-${index}`
    );
  }

  function conversationUsername(node) {
    const named = q(SELECTORS.conversationName, node);
    if (named) return text(named, 120);
    // Rows without a nickname node put the username on the first text line.
    const lines = ((node.innerText || node.textContent || "").split("\n"))
      .map((line) => line.replace(/\s+/g, " ").trim())
      .filter(Boolean);
    return (lines[0] || "").slice(0, 120);
  }

  function conversationPreview(node) {
    const preview = q(SELECTORS.conversationPreview, node);
    if (preview) return text(preview, 300);
    const lines = ((node.innerText || node.textContent || "").split("\n"))
      .map((line) => line.replace(/\s+/g, " ").trim())
      .filter(Boolean);
    return lines.slice(1).join(" ").slice(0, 300);
  }

  function isUnread(node) {
    if (attr(node, "data-unread") === "true" || node.dataset?.unread === "true") return true;
    if (UNREAD_HINT.test(attr(node, "aria-label"))) return true;
    for (const selector of SELECTORS.badge) {
      let badge = null;
      try {
        badge = node.querySelector(selector);
      } catch (_) {
        badge = null;
      }
      if (!badge || !visible(badge)) continue;
      // A bare <sup>/badge element only counts when it actually shows a count
      // or is explicitly labelled unread.
      if (digits(text(badge, 10)) > 0) return true;
      if (UNREAD_HINT.test(attr(badge, "aria-label"))) return true;
      if (attr(badge, "data-unread") === "true") return true;
    }
    return false;
  }

  function isMessageRequest(node) {
    if (REQUEST_HINT.test(attr(node, "aria-label"))) return true;
    if (REQUEST_HINT.test(attr(node, "data-e2e"))) return true;
    const section = node.closest('[data-e2e*="request" i],[aria-label*="request" i]');
    return Boolean(section);
  }

  // ---------------------------------------------------------------------------
  // Navigation badges
  // ---------------------------------------------------------------------------

  function navNode(list) {
    const panel = findMessagesPanel();
    for (const selector of list) {
      let nodes = [];
      try {
        nodes = [...document.querySelectorAll(selector)];
      } catch (_) {
        nodes = [];
      }
      const found = nodes.find((node) => !(panel && panel.contains(node)) && visible(node));
      if (found) return found;
    }
    return null;
  }

  function badgeCountOf(navRoot) {
    if (!navRoot) return 0;
    const label = attr(navRoot, "aria-label");
    let best = UNREAD_HINT.test(label) ? Math.max(1, digits(label)) : 0;
    for (const selector of SELECTORS.badge) {
      let nodes = [];
      try {
        nodes = [...navRoot.querySelectorAll(selector)];
      } catch (_) {
        nodes = [];
      }
      for (const node of nodes) {
        if (!visible(node)) continue;
        const count = digits(text(node, 12)) || digits(attr(node, "aria-label"));
        if (count > best) best = count;
        else if (!best && (attr(node, "data-unread") === "true" || UNREAD_HINT.test(attr(node, "aria-label")))) {
          best = 1;
        }
      }
      if (best) break;
    }
    return best;
  }

  const messagesNavNode = () => navNode(SELECTORS.messagesNav);
  const activityNavNode = () => navNode(SELECTORS.activityNav);

  function messagesUnreadCount() {
    return badgeCountOf(messagesNavNode());
  }

  function activityUnreadCount() {
    return badgeCountOf(activityNavNode());
  }

  // ---------------------------------------------------------------------------
  // Conversation surface
  // ---------------------------------------------------------------------------

  function activeChatNode() {
    const header = q(SELECTORS.conversationHeader);
    return header && visible(header) ? header : null;
  }

  // Direct selectors first (most stable); falls back to climbing a bounded
  // number of ancestors from the nickname node and taking the first visible
  // <img> found - TikTok renders the avatar as a nearby sibling of the
  // nickname inside the same header container, so this survives a class
  // name rename that would break a hardcoded selector.
  function activeChatAvatarUrl() {
    const direct = q(SELECTORS.conversationHeaderAvatar);
    if (direct && visible(direct)) {
      const src = attr(direct, "src");
      if (src) return src;
    }
    let node = activeChatNode();
    for (let depth = 0; node && depth < 5; depth += 1) {
      const img = node.querySelector && node.querySelector("img");
      if (img && visible(img)) {
        const src = attr(img, "src");
        if (src) return src;
      }
      node = node.parentElement;
    }
    return "";
  }

  function composerNode() {
    const node = q(SELECTORS.messageInput);
    return node && visible(node) ? node : null;
  }

  // Only chat boxes associated with the visible composer can contain the
  // active thread. TikTok renders Messages as a drawer over the feed, so a
  // document-wide message search can otherwise ingest unrelated feed nodes.
  function activeChatBoxes() {
    const composer = composerNode();
    if (!composer) return [];

    const composerRect = composer.getBoundingClientRect();
    return qa(SELECTORS.chatBox, document, 8)
      .filter(visible)
      .filter((box) => {
        const rect = box.getBoundingClientRect();
        const horizontalOverlap =
          Math.min(rect.right, composerRect.right) -
          Math.max(rect.left, composerRect.left);
        return (
          box.contains(composer) ||
          (horizontalOverlap > 0 && rect.top < composerRect.top)
        );
      });
  }

  // A short, stable fingerprint used only when TikTok renders a message
  // without any id attribute. It is derived from content and DOM position, so
  // the same rendered message keeps the same id across snapshots.
  function fingerprint(value) {
    let out = 5381;
    for (let i = 0; i < value.length; i += 1) out = ((out * 33) ^ value.charCodeAt(i)) >>> 0;
    return out.toString(16);
  }

  function isNonMessageMedia(element) {
    if (!element || !element.closest) return true;
    const semantic = `${attr(element, "data-e2e")} ${attr(element, "class")} ` +
      `${attr(element, "aria-label")} ${attr(element, "role")}`.toLowerCase();
    if (/(avatar|emoji|sticker-picker|profile-photo|profile image)/.test(semantic)) return true;
    if (element.closest(
      '[data-e2e*="avatar" i],[class*="Avatar" i],' +
      '[data-e2e*="emoji" i],[class*="Emoji" i],' +
      '[data-e2e*="sticker-picker" i]'
    )) return true;
    const composer = composerNode();
    if (composer && composer.contains(element)) {
      return true;
    }
    if (element.closest(
      '[data-e2e="dm-new-input-editor"],[data-e2e="message-input-area"]'
    )) return true;
    if (element.closest('[role="dialog"]')) return true;
    return false;
  }

  function mediaElements(node) {
    if (!node) return [];
    const elements = [];
    if (/^(IMG|VIDEO)$/.test(node.tagName || "")) elements.push(node);
    if (node.querySelectorAll) elements.push(...node.querySelectorAll("img, video"));
    return [...new Set(elements)].filter((element) => {
      if (isNonMessageMedia(element)) return false;
      const box = element.getBoundingClientRect();
      return box.width >= 40 && box.height >= 40;
    });
  }

  // Images and videos rendered inside one message bubble. The `src` is what
  // makes two different pictures distinguishable, so it doubles as the identity
  // signature for a message that carries no text at all.
  function messageMedia(node) {
    const items = [];
    for (const element of mediaElements(node).slice(0, 8)) {
      const source =
        element.currentSrc ||
        element.getAttribute("src") ||
        element.getAttribute("data-src") ||
        element.getAttribute("poster") ||
        (element.querySelector && element.querySelector("source")
          ? element.querySelector("source").getAttribute("src")
          : "") ||
        "";
      const box = element.getBoundingClientRect();
      if (!source) continue;
      items.push({
        kind: element.tagName.toLowerCase() === "video" ? "video" : "image",
        src: source.slice(0, 500),
        width: Math.round(box.width),
        height: Math.round(box.height)
      });
    }
    return { items, signature: items.map((item) => item.src).join("|") };
  }

  function messageId(node, index, direction, body) {
    const explicit =
      attr(node, "data-message-id") ||
      attr(node, "data-msg-id");
    if (explicit) return explicit;
    // `data-id` and `data-key` on TikTok message wrappers are not guaranteed
    // to identify a bubble: some builds reuse the conversation/author key for
    // every message. Treating either as a native message id made a later
    // inbound look identical to the one settled by the previous scan.
    // Repeated customer messages are common ("Chưa thấy", "Bạn ơi", ...).
    // Content alone made two distinct bubbles share one id and caused the
    // settled ledger to discard the newer one. DOM position is part of the
    // fallback identity when TikTok exposes no native message id.
    return `dom-${fingerprint(`${index}|${direction}|${body}`)}`;
  }

  // Absolute time when TikTok exposes one; `null` otherwise. Callers must then
  // fall back to DOM order, which is always present.
  function messageTimestamp(node) {
    const raw =
      attr(node, "data-timestamp") ||
      attr(node, "data-time") ||
      attr(node, "datetime") ||
      (() => {
        const time = node.querySelector ? node.querySelector("time") : null;
        return time ? attr(time, "datetime") || attr(time, "data-timestamp") || attr(time, "title") : "";
      })();
    if (!raw) return null;
    const numeric = Number(raw);
    if (Number.isFinite(numeric) && numeric > 0) return numeric;
    const parsed = Date.parse(raw);
    return Number.isNaN(parsed) ? null : parsed;
  }

  // ---------------------------------------------------------------------------
  // Message direction
  //
  // Three sources, in decreasing order of trust:
  //
  //   1. DOM semantics TikTok states itself (`data-e2e`, `data-direction`,
  //      `aria-label`) on the bubble or one of the wrappers that belong to it
  //      alone;
  //   2. the layout the bubble is placed with (`align-self`, `margin-left:auto`,
  //      `flex-direction: row-reverse`, the row's `justify-content`), read from
  //      the computed style so build-hash class names never matter;
  //   3. where the bubble actually sits inside the thread - left is theirs,
  //      right is ours.
  //
  // Class names are consulted only as whole words and only after the layout,
  // because everything else in them is a build hash that changes per release.
  // `UNKNOWN` is returned only when all three say nothing, and it then carries
  // the DOM metadata needed to work out why.
  // ---------------------------------------------------------------------------

  const SELF_WORDS = new Set(["self", "own", "mine", "outgoing", "sent", "sender"]);
  const OTHER_WORDS = new Set(["other", "others", "friend", "partner", "incoming", "received", "opposite"]);

  // `DivChatItemSelf` -> ["div", "chat", "item", "self"], so a whole word can be
  // matched inside a camel-cased class without matching a hash fragment.
  function words(value) {
    return String(value || "")
      .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
      .toLowerCase()
      .split(/[^a-z]+/)
      .filter(Boolean);
  }

  function markerDirection(list) {
    for (const word of list) {
      if (SELF_WORDS.has(word)) return "OUTBOUND";
      if (OTHER_WORDS.has(word)) return "INBOUND";
    }
    return "UNKNOWN";
  }

  // The wrappers that belong to this message alone: from the bubble up to, but
  // not including, the first ancestor shared with another message. That is the
  // row TikTok aligns, whatever it happens to be called in this build.
  function ownChain(node, container, others) {
    const chain = [node];
    let current = node;
    for (let depth = 0; depth < 8; depth += 1) {
      const parent = current.parentElement;
      if (!parent || parent === container || parent === document.body) break;
      if (others.some((other) => other !== node && parent.contains(other))) break;
      chain.push(parent);
      current = parent;
    }
    return chain;
  }

  function semanticDirection(chain) {
    for (const element of chain) {
      const semantic = words(
        `${attr(element, "data-e2e")} ${attr(element, "data-direction")} ` +
        `${attr(element, "data-type")} ${attr(element, "data-role")} ${attr(element, "aria-label")}`
      );
      const direction = markerDirection(semantic);
      if (direction !== "UNKNOWN") return direction;
    }
    return "UNKNOWN";
  }

  // Strong layout evidence only: something the browser resolved to "this box is
  // pushed to one side". `justify-content: flex-start` is deliberately absent -
  // it is the default of every flex container and would read every message as
  // inbound.
  function layoutDirection(chain) {
    for (const element of chain) {
      const style = getComputedStyle(element);
      if (style.alignSelf === "flex-end") return "OUTBOUND";
      if (style.alignSelf === "flex-start") return "INBOUND";
      if (style.marginLeft === "auto" && style.marginRight !== "auto") return "OUTBOUND";
      if (style.marginRight === "auto" && style.marginLeft !== "auto") return "INBOUND";
      const parent = element.parentElement;
      if (!parent) continue;
      const parentStyle = getComputedStyle(parent);
      if (!/flex/.test(parentStyle.display)) continue;
      if (parentStyle.flexDirection === "row-reverse") return "OUTBOUND";
      if (/(flex-end|right|end)/.test(parentStyle.justifyContent)) return "OUTBOUND";
      // `flex-start` is also the default of every flex container, so it only
      // counts when the row is genuinely wider than the bubble it is holding
      // to the left - a bubble that fills its row proves nothing.
      if (/(flex-start|left|start)/.test(parentStyle.justifyContent)) {
        const row = parent.getBoundingClientRect();
        const bubble = element.getBoundingClientRect();
        if (bubble.width > 0 && row.width > bubble.width * 1.15) return "INBOUND";
      }
    }
    return "UNKNOWN";
  }

  function classDirection(chain) {
    for (const element of chain) {
      const direction = markerDirection(words(attr(element, "class")));
      if (direction !== "UNKNOWN") return direction;
    }
    return "UNKNOWN";
  }

  // Where the bubble sits in the thread. Bubbles have a capped width, so the
  // aligned edge separates them: ours hugs the right edge, theirs the left.
  function geometryDirection(rect, frame) {
    if (!frame || frame.width <= 0 || !rect || rect.width <= 0) return "UNKNOWN";
    const margin = Math.max(8, frame.width * 0.04);
    const fromLeft = rect.left - frame.left;
    const fromRight = frame.right - rect.right;
    if (fromRight + margin < fromLeft) return "OUTBOUND";
    if (fromLeft + margin < fromRight) return "INBOUND";
    // A bubble wide enough to reach both edges is decided by its centre.
    const centre = rect.left + rect.width / 2;
    const frameCentre = frame.left + frame.width / 2;
    if (centre > frameCentre + margin) return "OUTBOUND";
    if (centre + margin < frameCentre) return "INBOUND";
    return "UNKNOWN";
  }

  function directionDebug(node, chain, rect, frame, body) {
    const parent = node.parentElement;
    return {
      tag: node.tagName ? node.tagName.toLowerCase() : "",
      class: attr(node, "class").slice(0, 200),
      data_e2e: attr(node, "data-e2e"),
      parent_tag: parent && parent.tagName ? parent.tagName.toLowerCase() : "",
      parent_class: parent ? attr(parent, "class").slice(0, 200) : "",
      parent_data_e2e: parent ? attr(parent, "data-e2e") : "",
      chain_depth: chain.length,
      rect_left: rect ? Math.round(rect.left) : null,
      rect_right: rect ? Math.round(rect.right) : null,
      rect_width: rect ? Math.round(rect.width) : null,
      container_left: frame ? Math.round(frame.left) : null,
      container_right: frame ? Math.round(frame.right) : null,
      container_center: frame ? Math.round(frame.left + frame.width / 2) : null,
      text: String(body || "").slice(0, 80)
    };
  }

  function resolveDirection(node, chain, frame, body) {
    const rect = node.getBoundingClientRect();
    const semantic = semanticDirection(chain);
    if (semantic !== "UNKNOWN") return { direction: semantic, source: "dom_semantics" };
    const layout = layoutDirection(chain);
    if (layout !== "UNKNOWN") return { direction: layout, source: "layout" };
    const byClass = classDirection(chain);
    if (byClass !== "UNKNOWN") return { direction: byClass, source: "class_word" };
    // The widest wrapper that is still this message's own is the bubble as it
    // is laid out; the bare text node inside it can be narrower than the box
    // that is actually aligned.
    const box = chain.reduce((widest, element) => {
      const candidate = element.getBoundingClientRect();
      return candidate.width > widest.width && frame && candidate.width < frame.width * 0.95
        ? candidate : widest;
    }, rect);
    const geometry = geometryDirection(box, frame);
    if (geometry !== "UNKNOWN") return { direction: geometry, source: "position" };
    return {
      direction: "UNKNOWN",
      source: "unresolved",
      debug: directionDebug(node, chain, rect, frame, body)
    };
  }

  // A banner TikTok itself renders inside the thread ("Message request
  // accepted...", a date separator): full width and centred, with no side to
  // it. It is not a message, and treating it as one is what made a thread of
  // five customer messages read as "one message, direction UNKNOWN" - and,
  // separately, made an accepted request's whole thread render as nothing
  // but two duplicated "Message request accepted" lines on the dashboard.
  //
  // Two different strengths of evidence are combined here, deliberately not
  // gated the same way:
  //  - exact banner text and semantic role/data-e2e/aria-label hints are
  //    near-unambiguous, so they apply unconditionally. A banner that
  //    inherited a real bubble's wrapper classes and got a spurious resolved
  //    `direction` must still be screened out by these, or it gets written
  //    to the database like a real chat message.
  //  - a centred or near-full-width *wrapper* is not unambiguous: an
  //    ordinary bubble's own row wrapper is routinely centred/full-width too
  //    (a narrow panel especially), and trusting this for every candidate
  //    turned a real, fully-resolved thread into "0 messages, all notices".
  //    It is only trusted when the direction resolver itself came up empty -
  //    exactly the ambiguous case this heuristic was written for.
  const SYSTEM_NOTICE_RE =
    /^message request accepted(?:\.\s*you can start chatting\.)?$/i;

  function isSystemNotice(node, chain, frame, body, direction) {
    const normalized = String(body || "").replace(/\s+/g, " ").trim();
    if (SYSTEM_NOTICE_RE.test(normalized)) return true;

    // Notice semantics can live on an outer wrapper rather than the text
    // leaf, so inspect this candidate's own wrapper chain.
    for (const element of chain) {
      const semantic = [
        attr(element, "role"),
        attr(element, "data-e2e"),
        attr(element, "aria-label")
      ].join(" ").toLowerCase();
      if (/(system|notice|status|date-separator|timestamp)/.test(semantic)) {
        return true;
      }
    }
    if (direction !== "UNKNOWN") return false;
    for (const element of chain) {
      const style = getComputedStyle(element);
      if (style.textAlign === "center") return true;
      if (frame && frame.width > 0) {
        const rect = element.getBoundingClientRect();
        if (rect.width >= frame.width * 0.85) return true;
      }
    }
    return false;
  }

  // Who wrote the message, when the DOM says so. The header username is the
  // fallback for a received message: it is the customer whose thread is open.
  function messageSender(chain, direction) {
    for (const element of chain.slice(0, 3)) {
      const named = element.querySelector
        ? element.querySelector('[data-e2e*="nickname" i], [data-e2e*="uniqueid" i], [data-e2e*="user-name" i]')
        : null;
      const value = named ? text(named, 80) : "";
      if (value) return value;
      // An avatar's `alt` names the sender; a picture the message carries
      // has an `alt` too ("image message"), and it names nothing.
      const media = mediaElements(element);
      const avatar = element.querySelectorAll
        ? [...element.querySelectorAll("img[alt]")].find((img) => !media.includes(img))
        : null;
      const alt = avatar ? String(avatar.getAttribute("alt") || "").trim() : "";
      if (alt && alt.length < 60) return alt;
    }
    if (direction === "OUTBOUND") return "self";
    const header = activeChatNode();
    return direction === "INBOUND" && header ? text(header, 80) : "";
  }

  // ---------------------------------------------------------------------------
  // The thread and its reference frame
  // ---------------------------------------------------------------------------

  function scrollableAncestor(node) {
    for (let current = node && node.parentElement, depth = 0; current && depth < 12; current = current.parentElement, depth += 1) {
      const style = getComputedStyle(current);
      if (/(auto|scroll)/.test(`${style.overflowY} ${style.overflow}`) && current.clientHeight > 0) {
        return current;
      }
    }
    return null;
  }

  function commonAncestor(nodes) {
    if (!nodes.length) return null;
    let current = nodes[0].parentElement;
    for (let depth = 0; current && depth < 16; current = current.parentElement, depth += 1) {
      if (nodes.every((node) => current.contains(node))) return current;
    }
    return null;
  }

  // The bubbles, innermost match per message. A selector can match both a
  // wrapper and the text element inside it - and in some builds a wrapper that
  // holds the *whole* thread, which used to collapse every message into one.
  // Keeping only nodes that contain no other match is what makes the count
  // right; the wrappers are recovered per message by `ownChain`.
  // The scrollable region above the composer: what a thread is, structurally,
  // when TikTok exposes no `data-e2e` for it at all.
  function threadContainerFallback() {
    const composer = composerNode();
    if (!composer) return null;
    for (let current = composer.parentElement, depth = 0; current && depth < 8; current = current.parentElement, depth += 1) {
      const scrollable = Array.from(current.querySelectorAll("*")).filter((element) => {
        if (element.contains(composer) || element.clientHeight <= 40) return false;
        const style = getComputedStyle(element);
        return /(auto|scroll)/.test(`${style.overflowY} ${style.overflow}`);
      });
      if (scrollable.length) return scrollable[scrollable.length - 1];
    }
    return null;
  }

  // The deepest elements that carry text of their own. A message bubble always
  // is one; a wrapper never is, because its text lives in a child.
  function textLeaves(container, limit = 200) {
    if (!container || !container.querySelectorAll) return [];
    return Array.from(container.querySelectorAll("*"))
      .filter((element) => {
        if (!text(element, 20)) return false;
        return !Array.from(element.children).some((child) => text(child, 20));
      })
      .slice(-limit);
  }

  // Filled in by `messages()`, read back by `threadInfo()`.
  let lastThread = { container: "none", nodes: 0, notices: 0, frame_width: null };

  // Bubbles inside one root, innermost match per message. A selector can match
  // a wrapper, the text element inside it, and in some builds a wrapper holding
  // the whole thread - keeping only matches that contain no other match is what
  // makes the count right. The wrappers are recovered per message by `ownChain`.
  function candidatesIn(root) {
    let nodes = qa(SELECTORS.messageItem, root, 200);
    if (!nodes.length) nodes = qa(SELECTORS.messageText, root, 200);
    nodes = nodes.filter(
      (node) => !nodes.some((other) => other !== node && node.contains(other))
    );
    // TikTok media-only bubbles do not consistently carry message-item or
    // message-text markers. Add their image/video node as the innermost bubble;
    // ownChain() recovers the aligned row for direction without swallowing the
    // whole thread. Media already contained by a marked bubble is not duplicated.
    for (const media of mediaElements(root)) {
      if (!nodes.some((node) => node.contains(media))) nodes.push(media);
    }
    return [...new Set(nodes)].sort((left, right) => {
      if (left === right) return 0;
      return left.compareDocumentPosition(right) & Node.DOCUMENT_POSITION_FOLLOWING
        ? -1 : 1;
    });
  }

  // Every place the thread could be, cheapest and most trustworthy first.
  // `messages()` walks these in order until one yields a real message, because
  // TikTok re-renders a conversation into a new container and leaves the old
  // one in the page holding nothing but a banner - reading only the first
  // marked container is how six customer messages turned into "the thread
  // rendered no messages".
  //
  // Each level is a function, not a value: the later ones walk the whole page,
  // and the extractor runs on every DOM mutation, so they must not be paid for
  // while the marked container is doing its job.
  const MESSAGE_SOURCES = [
    // What TikTok marks as a thread. Several can be in the page at once: a
    // re-render leaves the old container in the page next to the new one,
    // and the old one is consistently the one that appears EARLIER in DOM
    // order (it was there first; the fresh container is what TikTok just
    // inserted to replace it). Node count is not a safe way to tell them
    // apart - a stale container can hold a longer conversation than a live
    // one that has only just started rendering, and picking "most nodes"
    // then locks onto old, already-superseded content (confirmed against a
    // production thread that kept reading one old sent image forever while
    // the customer's new message sat in the live, shorter container next to
    // it). DOM position - last one wins - is the reliable signal; node count
    // only breaks a tie between containers at the same position.
    () => qa(SELECTORS.messageList, document, 8)
      .filter(visible)
      .map((list) => ({ list, source: "message_list", nodes: candidatesIn(list) }))
      .filter((item) => item.nodes.length)
      .sort((left, right) => {
        if (left.list !== right.list) {
          // `right` appearing later in the DOM than `left` means `right` is
          // the fresher container and must sort first (negative diff moves
          // an element earlier in the array).
          const position = left.list.compareDocumentPosition(right.list);
          if (position & Node.DOCUMENT_POSITION_FOLLOWING) return 1;
          if (position & Node.DOCUMENT_POSITION_PRECEDING) return -1;
        }
        return right.nodes.length - left.nodes.length;
      }),

    // A visible chat box tied to the composer. This replaces the unsafe
    // document-wide fallback that crossed the Messages drawer edge.
    () => activeChatBoxes()
      .map((list) => ({ list, source: "chat_box", nodes: candidatesIn(list) }))
      .filter((item) => item.nodes.length)
      .sort((left, right) => right.nodes.length - left.nodes.length),

    // Nothing TikTok marks as a message is in the page. Rather than reporting
    // an empty thread - which reads as "the customer wrote nothing" - the
    // bubbles are recovered from the structure of the thread itself.
    () => {
      const container = threadContainerFallback();
      const leaves = [...new Set([
        ...textLeaves(container),
        ...mediaElements(container)
      ])].sort((left, right) => (
        left.compareDocumentPosition(right) & Node.DOCUMENT_POSITION_FOLLOWING
          ? -1 : 1
      ));
      return leaves.length
        ? [{ list: container, source: "structure", nodes: leaves }]
        : [];
    }
  ];

  function unionRect(nodes) {
    const boxes = nodes.map((node) => node.getBoundingClientRect()).filter((box) => box.width > 0);
    if (!boxes.length) return null;
    const left = Math.min(...boxes.map((box) => box.left));
    const right = Math.max(...boxes.map((box) => box.right));
    return { left, right, width: right - left };
  }

  // The box "left" and "right" are measured against. Without one there is no
  // way to tell the sides apart, which is how a thread whose list container
  // TikTok did not expose ended up entirely UNKNOWN.
  //
  // A frame narrower than the bubbles it is supposed to contain is worse than
  // none: it turns every position into nonsense. Such a candidate is skipped,
  // and when nothing qualifies the answer is "no frame" - the direction then
  // stays UNKNOWN honestly instead of being invented.
  function threadFrame(list, nodes) {
    const composer = composerNode();
    const candidates = [
      list,
      scrollableAncestor(nodes[0]),
      commonAncestor(nodes),
      threadContainerFallback(),
      composer ? composer.parentElement : null
    ];
    const boxes = nodes.map((node) => node.getBoundingClientRect());
    // A banner fills the thread by design, so it must not be counted as a
    // bubble here: doing so made every candidate look too narrow and switched
    // the position fallback off for the whole thread.
    const wideEnough = (width, content) =>
      width > Math.max(content * 1.15, content + 24);
    for (const candidate of candidates) {
      if (!candidate) continue;
      const rect = candidate.getBoundingClientRect();
      if (rect.width <= 0) continue;
      const content = boxes.reduce(
        (value, box) => (box.width < rect.width * 0.85 ? Math.max(value, box.width) : value), 0
      );
      if (wideEnough(rect.width, content)) return rect;
    }
    // The bubbles themselves span the thread when both sides are represented.
    const union = unionRect(nodes);
    const widest = boxes.reduce((value, box) => Math.max(value, box.width), 0);
    return union && wideEnough(union.width, widest) ? union : null;
  }

  // One thread source, parsed. Kept beside the parser helpers so
  // `messages()` above reads as the decision it is.
  function readThread({ list, nodes, source }, limit) {
      const frame = threadFrame(list, nodes);
      lastThread = {
        container: source,
        container_class: list ? attr(list, "class").slice(0, 120) : "",
        attempted: "",
        nodes: nodes.length,
        notices: 0,
        frame_width: frame ? Math.round(frame.width) : null
      };
      const offset = Math.max(0, nodes.length - limit);
      return nodes
        .slice(offset)
        .map((node, position) => {
          const index = offset + position;
          const body = q(SELECTORS.messageText, node) || node;
          const value = text(body, 4000);
          const media = messageMedia(node);
          const chain = ownChain(node, list, nodes);
          const resolved = resolveDirection(node, chain, frame, value || media.signature);
          const notice = isSystemNotice(node, chain, frame, value, resolved.direction);
          if (notice) lastThread.notices += 1;
          return {
            node,
            system: notice,
            // A media message has no text, so identity must come from what it
            // actually contains. Without this every image would hash to the
            // same id and dedup would treat them as one message.
            id: messageId(node, index, resolved.direction, value || media.signature),
            index,
            text: value,
            media: media.items,
            direction: resolved.direction,
            direction_source: resolved.source,
            sender: messageSender(chain, resolved.direction),
            // Only present when the direction could not be resolved: it is the
            // DOM evidence a human needs to add the missing rule.
            debug: resolved.debug || null,
            timestamp: messageTimestamp(node)
          };
        })
        // A message with neither text nor media is a layout artefact, not
        // something a customer sent. Media-only messages are kept: dropping
        // them made the agent blind to images in both directions. A banner the
        // platform wrote is not a message either.
        .filter((item) => (item.text || item.media.length) && !item.system);
  }

  globalThis.AgentPlatforms.register({
    id: "tiktok_web",
    matches: (loc) => /(^|\.)tiktok\.com$/.test(loc.hostname),
    selectors: SELECTORS,

    // DOM semantics decide the page. The URL is only consulted when the DOM
    // proves nothing, because the inbox drawer opens without changing it.
    detectPage() {
      if (q(SELECTORS.captcha)) return { page: "security_challenge", challenge: "captcha", confidence: 1 };
      if (q(SELECTORS.otp)) return { page: "security_challenge", challenge: "otp", confidence: 0.97 };
      if (document.querySelector('input[type="password"]') || location.pathname.startsWith("/login")) {
        return { page: "login", confidence: 0.96 };
      }

      const panel = panelState();
      const chat = activeChatNode();
      const composer = composerNode();

      // A conversation needs strong DOM evidence: an active chat header AND a
      // composer. URL is irrelevant.
      if (chat && composer) {
        return {
          page: "conversation",
          conversation_open: true,
          confidence: 0.97,
          messages_mode: panel.mode,
          messages_panel: panel
        };
      }
      if (panel.present && panel.visible) {
        return {
          page: "messages",
          conversation_open: false,
          confidence: 0.95,
          messages_mode: panel.mode,
          messages_panel: panel
        };
      }
      if (composer && q(SELECTORS.messageList)) {
        return { page: "conversation", conversation_open: true, confidence: 0.9, messages_mode: panel.mode };
      }

      const path = location.pathname;
      if (path.startsWith("/messages")) return { page: "messages", confidence: 0.6, messages_mode: "page" };
      if (path.startsWith("/search")) return { page: "search", confidence: 0.9 };
      if (/^\/@[^/]+/.test(path)) return { page: "profile", confidence: 0.9 };
      if (path === "/" || path.startsWith("/foryou") || path.startsWith("/following") || path.startsWith("/explore")) {
        return { page: "home", confidence: 0.9 };
      }
      return { page: "unknown", confidence: 0.2 };
    },

    // Logical roles the agent may address. Generic code never sees a selector.
    roles: {
      messages_nav: () => [messagesNavNode()].filter(Boolean),
      activity_nav: () => [activityNavNode()].filter(Boolean),
      messages_panel: () => [panelState().container].filter(Boolean),
      conversation: () => conversationRows(),
      message_input: () => [composerNode()].filter(Boolean),
      send_button: () => [q(SELECTORS.sendButton)].filter(Boolean),
      back_button: () => [q(SELECTORS.backButton)].filter(Boolean),
      chat_header: () => [activeChatNode()].filter(Boolean),
      modal: () => qa(SELECTORS.modal, document, 5),
      modal_close: () => {
        const modal = q(SELECTORS.modal);
        return modal ? [q(SELECTORS.modalClose, modal)].filter(Boolean) : [];
      }
    },

    describe: {
      conversation(node, index) {
        const username = conversationUsername(node);
        return {
          key: conversationKey(node, index),
          username,
          text: username,
          preview: conversationPreview(node),
          unread: isUnread(node),
          message_request: isMessageRequest(node),
          visible: visible(node)
        };
      },
      messageInput(node) {
        return {
          text: node.isContentEditable ? text(node, 4000) : String(node.value || "").slice(0, 4000),
          editable: Boolean(node.isContentEditable || node.tagName === "TEXTAREA" || node.tagName === "INPUT")
        };
      },
      messagesNav() {
        const count = messagesUnreadCount();
        return {
          // The nav descriptor carries the Messages badge only, never Activity.
          text: count > 0 ? String(count) : "",
          unread: count > 0,
          unread_count: count
        };
      },
      activityNav() {
        const count = activityUnreadCount();
        return { text: count > 0 ? String(count) : "", unread: count > 0, unread_count: count };
      }
    },

    messagesPanel: panelState,

    activeChat() {
      const node = activeChatNode();
      const username = node ? text(node, 120) : "";
      const avatarUrl = node ? activeChatAvatarUrl() : "";
      return {
        username: username || null,
        present: Boolean(node),
        avatar_url: avatarUrl || null,
      };
    },

    messagesUnreadCount,
    activityUnreadCount,

    // Kept for callers that only want one number: Messages only, so an
    // Activity badge can never trigger the inbox flow.
    unreadCount() {
      const fromBadge = messagesUnreadCount();
      const fromList = conversationRows().filter(isUnread).length;
      return Math.max(fromBadge, fromList);
    },

    // The thread as rendered, oldest first. `index` is the position in the
    // full message list, so "which message came after which" is answerable
    // from DOM order alone when TikTok exposes no timestamp.
    // What the parser saw while reading the thread. Diagnostics only: it makes
    // "the thread rendered nothing" and "the thread was parsed wrong" tell
    // themselves apart from a log line.
    threadInfo() {
      return lastThread;
    },

    // The thread as rendered, oldest first. Every place it could be is tried in
    // turn and the first one holding a real message wins: a container left
    // behind by a re-render holds only a banner, and answering "no messages"
    // from it would leave the customer waiting.
    messages(limit = 40) {
      const attempted = [];
      let first = null;
      for (const build of MESSAGE_SOURCES) {
        for (const candidate of build()) {
          const parsed = readThread(candidate, limit);
          attempted.push(`${candidate.source}:${parsed.length}`);
          if (first === null) first = lastThread;
          if (parsed.length) {
            lastThread.attempted = attempted.join(",");
            return parsed;
          }
        }
      }
      // Nothing anywhere held a message. The first attempt is the informative
      // one - it says what was in the container TikTok marked.
      lastThread = first || { container: "none", nodes: 0, notices: 0, frame_width: null };
      lastThread.attempted = attempted.join(",") || "none";
      return [];
    },

    securityChallenge() {
      if (q(SELECTORS.captcha)) return { type: "captcha", requires_human: true };
      if (q(SELECTORS.otp)) return { type: "otp", requires_human: true };
      if (location.pathname.startsWith("/login") && q(SELECTORS.login)) {
        return { type: "login_required", requires_human: true };
      }
      return null;
    }
  });
})();

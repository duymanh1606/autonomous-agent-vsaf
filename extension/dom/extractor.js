(() => {
  "use strict";

  // Turns the live DOM into a compact structured state (schema v2). Raw HTML
  // never leaves the page: only semantic roles, short text and geometry are
  // exported.
  const SCHEMA_VERSION = 2;
  const LIMITS = { conversations: 40, messages: 30, elements: 80, text: 400, messageText: 2000 };

  const locator = () => globalThis.AgentLocator;

  function hash(value) {
    let out = 5381;
    const text = typeof value === "string" ? value : JSON.stringify(value);
    for (let i = 0; i < text.length; i += 1) out = ((out * 33) ^ text.charCodeAt(i)) >>> 0;
    return out.toString(16);
  }

  function describeRole(node, id, role, extra) {
    return locator().describe(node, id, role, extra);
  }

  function exportElement(item) {
    return {
      id: item.id,
      role: item.role,
      resource_id: item.resource_id || "",
      description: item.description || "",
      href: item.href || "",
      text: item.text,
      unread: item.unread ?? null,
      selector: item.selector,
      visible: item.visible,
      enabled: item.enabled,
      rect: item.rect
    };
  }

  function extract(sequence = 0) {
    const profile = globalThis.AgentPlatforms.match();
    if (!profile) return null;

    locator().reset();
    const detected = globalThis.AgentStateDetector.detect(profile);
    const elements = [];

    const pushRole = (role, id, extra = () => ({})) => {
      const node = (profile.roles[role]?.() || [])[0];
      if (!node) return null;
      const descriptor = describeRole(node, id, role, extra(node) || {});
      elements.push(descriptor);
      return descriptor;
    };

    const navDescriptor = pushRole("messages_nav", "messages_nav", () =>
      profile.describe.messagesNav ? profile.describe.messagesNav() : {}
    );
    pushRole("activity_nav", "activity_nav", () =>
      profile.describe.activityNav ? profile.describe.activityNav() : {}
    );
    const panelDescriptor = pushRole("messages_panel", "messages_panel");

    // Conversation rows come from the Messages panel/page container only; the
    // platform profile refuses to return navigation entries here.
    const conversationNodes = (profile.roles.conversation?.() || []).slice(0, LIMITS.conversations);
    const conversations = conversationNodes
      .map((node, index) => {
        const info = profile.describe.conversation(node, index);
        const username = String(info.username || info.text || "").trim();
        const preview = String(info.preview || "").trim();
        if (!username && !preview) return null;
        const id = `conversation_${index + 1}`;
        const descriptor = describeRole(node, id, "conversation", {
          text: username.slice(0, LIMITS.text),
          unread: Boolean(info.unread),
          preview: preview.slice(0, LIMITS.text),
          username: username.slice(0, LIMITS.text),
          message_request: Boolean(info.message_request),
          conversation_key: info.key
        });
        elements.push(descriptor);
        return {
          id,
          key: info.key,
          username: descriptor.username,
          text: descriptor.text,
          preview: descriptor.preview,
          unread: Boolean(info.unread),
          message_request: Boolean(info.message_request),
          visible: Boolean(descriptor.visible),
          selector: descriptor.selector,
          rect: descriptor.rect
        };
      })
      .filter(Boolean);

    const inputDescriptor = pushRole("message_input", "message_input", (node) =>
      profile.describe.messageInput(node)
    );
    const sendDescriptor = pushRole("send_button", "send_button");
    const headerDescriptor = pushRole("chat_header", "chat_header");
    pushRole("back_button", "back_button");
    pushRole("modal_close", "modal_close");

    const modalNode = (profile.roles.modal?.() || [])[0];
    const modal = modalNode
      ? { present: true, text: locator().textOf(modalNode, 600), rect: locator().rectOf(modalNode) }
      : null;

    // `index` is the position inside the rendered thread and `timestamp` is
    // whatever absolute time TikTok exposed. Together they let the agent decide
    // which message arrived after which without guessing.
    const messages = profile.messages(LIMITS.messages).map((item, position) => ({
      id: item.id,
      message_id: item.id,
      index: Number.isInteger(item.index) ? item.index : position,
      text: item.text.slice(0, LIMITS.messageText),
      direction: item.direction,
      // How the direction was established, and - when it was not - the DOM
      // evidence the agent logs so the missing rule can be added.
      direction_source: item.direction_source || "",
      sender: item.sender || "",
      media: item.media || [],
      direction_debug: item.debug || null,
      timestamp: item.timestamp ?? null
    }));

    // How the thread was read, so a log line can tell "nothing rendered" from
    // "parsed wrong". Diagnostics only; no decision is made on it.
    const thread = profile.threadInfo ? profile.threadInfo() : null;

    const activeChat = profile.activeChat
      ? profile.activeChat()
      : { username: null, present: false, avatar_url: null };
    const messagesUnread = profile.messagesUnreadCount ? profile.messagesUnreadCount() : 0;
    const activityUnread = profile.activityUnreadCount ? profile.activityUnreadCount() : 0;
    const panel = detected.messages_panel || { present: false, visible: false, mode: null };

    const state = {
      schema_version: SCHEMA_VERSION,
      platform: profile.id,
      sequence,
      captured_at: new Date().toISOString(),
      url: location.href,
      viewport: {
        width: Math.round(innerWidth),
        height: Math.round(innerHeight),
        dpr: Number(devicePixelRatio || 1)
      },
      title: document.title.slice(0, 200),
      page: detected.page,
      ui_state: detected.ui_state,
      page_confidence: detected.confidence,
      security_challenge: detected.challenge,
      messages_mode: detected.messages_mode,
      messages_panel: {
        present: Boolean(panel.present),
        visible: Boolean(panel.visible),
        mode: panel.mode || null,
        rect: panelDescriptor ? panelDescriptor.rect : null,
        selector: panelDescriptor ? panelDescriptor.selector : null
      },
      // Messages and Activity badges stay separate and are never summed.
      messages_unread_count: messagesUnread,
      activity_unread_count: activityUnread,
      unread_count: messagesUnread,
      active_chat: {
        username: activeChat.username || null,
        present: Boolean(activeChat.present),
        avatar_url: activeChat.avatar_url || null,
        selector: headerDescriptor ? headerDescriptor.selector : null
      },
      new_message: false,
      conversations,
      messages,
      thread,
      modal,
      composer: inputDescriptor
        ? {
            id: inputDescriptor.id,
            present: true,
            text: inputDescriptor.text,
            editable: Boolean(inputDescriptor.editable),
            visible: Boolean(inputDescriptor.visible),
            selector: inputDescriptor.selector,
            rect: inputDescriptor.rect
          }
        : { id: null, present: false, text: "", editable: false, visible: false, selector: null, rect: null },
      send_button: sendDescriptor
        ? {
            id: sendDescriptor.id,
            present: true,
            enabled: Boolean(sendDescriptor.enabled),
            visible: Boolean(sendDescriptor.visible),
            selector: sendDescriptor.selector,
            rect: sendDescriptor.rect
          }
        : { id: null, present: false, enabled: false, visible: false, selector: null, rect: null },
      messages_nav_present: Boolean(navDescriptor),
      elements: elements.slice(0, LIMITS.elements).map(exportElement)
    };

    // The hash covers semantics only, so scrolling or animation does not create
    // fake state changes. Actions carry it so a stale decision can be aborted.
    state.state_hash = hash({
      page: state.page,
      ui_state: state.ui_state,
      mode: state.messages_mode,
      unread: [state.messages_unread_count, state.activity_unread_count],
      active: state.active_chat.username,
      challenge: state.security_challenge,
      modal: modal ? modal.text : null,
      conversations: conversations.map((item) => [item.key, item.unread, item.preview]),
      messages: messages.map((item) => [item.id, item.direction, item.text]),
      composer: [state.composer.present, state.composer.text],
      send: [state.send_button.present, state.send_button.enabled]
    });
    return state;
  }

  globalThis.AgentExtractor = Object.freeze({ extract, hash, SCHEMA_VERSION, LIMITS });
})();

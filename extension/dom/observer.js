(() => {
  "use strict";

  // MutationObserver driven state watcher. It never polls the whole page, never
  // reloads and only emits when the normalised state really changed.
  const DEBOUNCE_MS = 250;
  const MIN_INTERVAL_MS = 120;

  let observer = null;
  let handler = () => {};
  let scheduled = null;
  let paused = 0;
  let sequence = 0;
  let previous = null;
  let lastEmit = 0;
  // Set by dom/notification_hook.js (a separate, MAIN-world content script -
  // see manifest.json) whenever the page itself calls `new Notification(...)`.
  // A native notification renders outside the page's own DOM, so nothing
  // here would otherwise ever notice one fired - this is the only signal
  // available for it, and the reason a real DOM mutation is forced below
  // instead of waiting for the next unrelated one.
  let notificationsSeen = 0;
  let lastNotification = null;

  window.addEventListener("autonomous-agent-notification-seen", (event) => {
    notificationsSeen += 1;
    lastNotification = (event && event.detail) || null;
    capture("notification", true);
  });

  const keyOf = (message) => `${message.id} ${message.direction} ${message.text}`;

  function diff(current) {
    if (!previous) return [{ event: "STATE_READY" }];
    const events = [];

    if (current.security_challenge && !previous.security_challenge) {
      events.push({ event: "SECURITY_CHALLENGE", challenge: current.security_challenge });
    }
    if (current.notifications_seen !== previous.notifications_seen) {
      // Forced by the listener above even when nothing else in the DOM
      // changed - without an event here, `capture()` never calls `handler`,
      // so this would never actually reach the Python side despite being
      // set on `state`.
      events.push({ event: "NOTIFICATION_SEEN", detail: current.last_notification });
    }
    if (current.page !== previous.page || current.ui_state !== previous.ui_state) {
      events.push({ event: "PAGE_CHANGED", from: previous.ui_state, to: current.ui_state });
    }
    if (current.messages_unread_count !== previous.messages_unread_count) {
      // Messages badge only. The Activity badge never drives the inbox flow.
      events.push({
        event: "UNREAD_CHANGED",
        from: previous.messages_unread_count,
        to: current.messages_unread_count
      });
    }
    if (
      Boolean(current.messages_panel?.visible) !== Boolean(previous.messages_panel?.visible)
    ) {
      events.push({
        event: current.messages_panel?.visible ? "MESSAGES_PANEL_OPENED" : "MESSAGES_PANEL_CLOSED",
        mode: current.messages_mode || null,
        conversations: current.conversations.length
      });
    }

    const previousConversations = new Map(previous.conversations.map((item) => [item.key, item]));
    const changed = current.conversations.filter((item) => {
      const before = previousConversations.get(item.key);
      return !before || before.preview !== item.preview || before.unread !== item.unread;
    });
    if (changed.length) {
      events.push({
        event: "CONVERSATION_LIST_CHANGED",
        conversations: changed.map((item) => ({ id: item.id, key: item.key, unread: item.unread }))
      });
      const newlyUnread = changed.filter(
        (item) => item.unread && !previousConversations.get(item.key)?.unread
      );
      for (const item of newlyUnread) {
        events.push({ event: "NEW_MESSAGE", conversation: item.key, conversation_id: item.id, source: "list" });
      }
    }

    const seen = new Set(previous.messages.map(keyOf));
    const fresh = current.messages.filter((message) => !seen.has(keyOf(message)));
    for (const message of fresh) {
      if (message.direction === "OUTBOUND") {
        events.push({ event: "MESSAGE_SENT", message_id: message.id, text: message.text });
      } else {
        events.push({
          event: "NEW_MESSAGE",
          conversation: current.active_chat?.username || null,
          message_id: message.id,
          text: message.text,
          source: "conversation"
        });
      }
    }

    const modalNow = Boolean(current.modal);
    const modalBefore = Boolean(previous.modal);
    if (modalNow && !modalBefore) events.push({ event: "MODAL_OPENED", text: current.modal.text });
    if (!modalNow && modalBefore) events.push({ event: "MODAL_CLOSED" });

    if (!events.length && current.state_hash !== previous.state_hash) {
      events.push({ event: "STATE_CHANGED" });
    }
    return events;
  }

  function capture(reason, force = false) {
    scheduled = null;
    if (paused > 0 && !force) return null;
    sequence += 1;
    const state = globalThis.AgentExtractor.extract(sequence);
    if (!state) return null;
    state.notifications_seen = notificationsSeen;
    state.last_notification = lastNotification;
    if (!force && previous && state.state_hash === previous.state_hash) return null;

    const events = diff(state);
    state.new_message = events.some((item) => item.event === "NEW_MESSAGE");
    previous = state;
    lastEmit = Date.now();
    if (events.length) {
      console.debug(
        "[agent] state=%s mode=%s seq=%s conversations=%s elements=%s",
        state.ui_state, state.messages_mode || "none", state.sequence,
        state.conversations.length, state.elements.length
      );
      handler({ reason, events, state });
    }
    return state;
  }

  function schedule(reason) {
    if (scheduled) return;
    const wait = Math.max(DEBOUNCE_MS, MIN_INTERVAL_MS - (Date.now() - lastEmit));
    scheduled = setTimeout(() => capture(reason), wait);
  }

  function hookNavigation() {
    for (const name of ["pushState", "replaceState"]) {
      const original = history[name];
      history[name] = function patched(...args) {
        const result = original.apply(this, args);
        schedule("navigation");
        return result;
      };
    }
    addEventListener("popstate", () => schedule("navigation"));
    addEventListener("hashchange", () => schedule("navigation"));
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) schedule("visibility");
    });
  }

  function start(onEvent) {
    handler = typeof onEvent === "function" ? onEvent : handler;
    if (observer) observer.disconnect();
    observer = new MutationObserver(() => schedule("mutation"));
    observer.observe(document.documentElement, {
      subtree: true,
      childList: true,
      characterData: true,
      attributes: true,
      attributeFilter: [
        "aria-label",
        "aria-hidden",
        "aria-disabled",
        "data-e2e",
        "data-unread",
        "class",
        "hidden",
        "disabled"
      ]
    });
    hookNavigation();
    capture("start", true);
  }

  // Actions mutate the DOM themselves; pausing keeps the executor from racing
  // its own mutations. State is always re-read once the action finished.
  function pause() {
    paused += 1;
  }

  function resume() {
    paused = Math.max(0, paused - 1);
  }

  globalThis.AgentObserver = Object.freeze({
    start,
    pause,
    resume,
    stop: () => observer && observer.disconnect(),
    snapshot: (reason = "manual") => capture(reason, true) || previous,
    lastState: () => previous
  });
})();

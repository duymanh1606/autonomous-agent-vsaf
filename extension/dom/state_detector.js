(() => {
  "use strict";

  // Observable UI states. This is the single vocabulary the extension exports;
  // every Python layer maps from exactly these names and never invents its own.
  // The agent-side machine owns the intent states (READING_MESSAGE,
  // GENERATING_REPLY, TYPING, SENDING, WAITING); the page can only prove where
  // the UI actually is right now.
  //
  // MESSAGES covers both shapes of the TikTok inbox. `messages_mode` says
  // which one: "panel" for the drawer overlaying the feed, "page" for the
  // full-width Messages route.
  const UI_STATE = Object.freeze({
    HOME: "HOME",
    MESSAGES: "MESSAGES",
    CONVERSATION: "CONVERSATION",
    PROFILE: "PROFILE",
    LOGIN: "LOGIN",
    SEARCH: "SEARCH",
    SECURITY_CHALLENGE: "SECURITY_CHALLENGE",
    UNKNOWN: "UNKNOWN"
  });

  const PAGE_TO_STATE = Object.freeze({
    home: UI_STATE.HOME,
    messages: UI_STATE.MESSAGES,
    conversation: UI_STATE.CONVERSATION,
    profile: UI_STATE.PROFILE,
    login: UI_STATE.LOGIN,
    search: UI_STATE.SEARCH,
    security_challenge: UI_STATE.SECURITY_CHALLENGE,
    unknown: UI_STATE.UNKNOWN
  });

  function emptyPanel() {
    return { present: false, visible: false, mode: null };
  }

  function panelOf(profile, page) {
    if (page && page.messages_panel) {
      const panel = page.messages_panel;
      return { present: Boolean(panel.present), visible: Boolean(panel.visible), mode: panel.mode || null };
    }
    if (typeof profile.messagesPanel !== "function") return emptyPanel();
    const panel = profile.messagesPanel();
    return { present: Boolean(panel.present), visible: Boolean(panel.visible), mode: panel.mode || null };
  }

  function detect(profile) {
    const page = profile.detectPage() || { page: "unknown", confidence: 0 };
    const challenge = profile.securityChallenge ? profile.securityChallenge() : null;
    const panel = panelOf(profile, page);

    if (challenge && challenge.type === "login_required") {
      return {
        page: page.page,
        ui_state: UI_STATE.LOGIN,
        confidence: 0.96,
        challenge,
        messages_mode: null,
        messages_panel: panel
      };
    }
    if (challenge) {
      return {
        page: page.page,
        ui_state: UI_STATE.SECURITY_CHALLENGE,
        confidence: 1,
        challenge,
        messages_mode: null,
        messages_panel: panel
      };
    }

    const composerPresent = (profile.roles.message_input() || []).length > 0;
    const chat = profile.activeChat ? profile.activeChat() : { present: false };

    let uiState = PAGE_TO_STATE[page.page] || UI_STATE.UNKNOWN;
    let confidence = Number(page.confidence || 0);

    // A conversation is only claimed on strong DOM evidence: the active chat
    // header plus a composer. The URL never decides this.
    if (uiState === UI_STATE.CONVERSATION && !(chat.present && composerPresent)) {
      uiState = panel.present && panel.visible ? UI_STATE.MESSAGES : UI_STATE.UNKNOWN;
      confidence = Math.min(confidence, 0.6);
    } else if (chat.present && composerPresent) {
      uiState = UI_STATE.CONVERSATION;
      confidence = Math.max(confidence, 0.97);
    } else if (uiState !== UI_STATE.MESSAGES && panel.present && panel.visible) {
      // The inbox drawer is open on top of the feed: that is MESSAGES.
      uiState = UI_STATE.MESSAGES;
      confidence = Math.max(confidence, 0.95);
    }

    return {
      page: page.page,
      ui_state: uiState,
      confidence,
      challenge: null,
      messages_mode: uiState === UI_STATE.MESSAGES || uiState === UI_STATE.CONVERSATION
        ? panel.mode || page.messages_mode || null
        : null,
      messages_panel: panel
    };
  }

  globalThis.AgentStateDetector = Object.freeze({ detect, UI_STATE, PAGE_TO_STATE });
})();

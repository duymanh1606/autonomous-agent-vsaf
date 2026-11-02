(() => {
  "use strict";

  // Single entry point for every action the agent may request.
  // Guarantees, in order:
  //   1. an action is rejected when the DOM changed after the agent decided
  //   2. the observer is paused so the action does not race its own mutations
  //   3. the DOM is always re-read afterwards and returned with the result
  const MAX_WAIT_MS = 5000;

  const HANDLERS = {
    click: (action) => globalThis.AgentClick.click(action.target),
    click_at: (action) => globalThis.AgentClick.clickAt(action.x, action.y),
    type: (action) => globalThis.AgentType.type(action.target, action.text, { clear: action.clear !== false }),
    press_enter: (action) => globalThis.AgentType.pressEnter(action.target),
    attach: (action) =>
      globalThis.AgentAttach.attach(action.target, action.media_url, action.media_name, action.media_mime),
    scroll_to: (action) => globalThis.AgentScroll.scrollTo(action.target, { block: action.block || "center" }),
    scroll_by: (action) => globalThis.AgentScroll.scrollBy(action.target, action.delta),
    scroll_to_latest: (action) => globalThis.AgentScroll.scrollToLatest(action.target),
    observe: async () => ({ ok: true, method: "observe" }),
    wait: async (action) => {
      const ms = Math.min(MAX_WAIT_MS, Math.max(0, Number(action.ms) || 0));
      await new Promise((resolve) => setTimeout(resolve, ms));
      return { ok: true, waited_ms: ms };
    },
    none: async () => ({ ok: true, method: "noop" }),
    halt: async (action) => ({ ok: true, method: "halt", reason: action.reason || "halted" })
  };

  function guard(action) {
    const current = globalThis.AgentObserver.lastState();
    if (!action || !action.action) return "INVALID_ACTION";
    if (!HANDLERS[action.action]) return "UNSUPPORTED_ACTION";
    if (action.expects_state_hash && current && action.expects_state_hash !== current.state_hash) {
      return "ACTION_ABORTED_STATE_CHANGED";
    }
    if (current && current.security_challenge && !["observe", "none", "halt"].includes(action.action)) {
      return "SECURITY_CHALLENGE_ACTIVE";
    }
    return null;
  }

  async function execute(action) {
    const started = Date.now();
    const observer = globalThis.AgentObserver;

    // The state the agent reasoned about may already be stale; re-read first.
    observer.snapshot("pre_action");
    const blocked = guard(action);
    if (blocked) {
      return {
        action_id: action?.action_id || null,
        action: action?.action || null,
        target: action?.target || null,
        ok: false,
        error: blocked,
        duration_ms: Date.now() - started,
        state: observer.lastState()
      };
    }

    observer.pause();
    let outcome;
    try {
      outcome = await HANDLERS[action.action](action);
    } catch (error) {
      outcome = { ok: false, error: "ACTION_EXCEPTION", message: String(error && error.message ? error.message : error) };
    } finally {
      observer.resume();
    }

    // Observe after every action: results are confirmed by the DOM, not assumed.
    const state = observer.snapshot("post_action");
    return {
      action_id: action.action_id || null,
      action: action.action,
      target: action.target || null,
      ok: Boolean(outcome && outcome.ok),
      error: (outcome && outcome.error) || null,
      details: outcome || null,
      duration_ms: Date.now() - started,
      state
    };
  }

  globalThis.AgentExecutor = Object.freeze({ execute, supported: Object.keys(HANDLERS) });
})();

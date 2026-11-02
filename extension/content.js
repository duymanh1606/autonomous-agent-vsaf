(() => {
  "use strict";

  // Page-side orchestrator.
  //   observer event -> background -> Python agent -> action -> executor
  //                                                        -> re-observe -> repeat
  // The page is never reloaded and navigation by URL is never performed here.
  const BRIDGE_ID = "autonomous-agent-perception-v1";
  const MAX_STEPS_PER_CYCLE = 12;

  let busy = false;
  let pending = null;

  function publishBridge(state, events = []) {
    // Read-only bridge the Playwright observer polls. It publishes exactly the
    // schema v2 state plus the event names that produced it, so the Python side
    // can tell a fresh state from a stale one without reloading anything.
    if (!state) return;
    let node = document.getElementById(BRIDGE_ID);
    if (!node) {
      node = document.createElement("script");
      node.id = BRIDGE_ID;
      node.type = "application/json";
      node.hidden = true;
      (document.documentElement || document).appendChild(node);
    }
    const payload = {
      ...state,
      events: (events || []).map((item) => String(item && item.event ? item.event : item))
    };
    node.textContent = JSON.stringify(payload);
    node.dataset.sequence = String(state.sequence || 0);
    node.dataset.uiState = String(state.ui_state || "UNKNOWN");
    node.dispatchEvent(new CustomEvent("autonomous-agent-perception", { bubbles: false }));
  }

  async function toBackground(message) {
    try {
      return await chrome.runtime.sendMessage(message);
    } catch (error) {
      // The service worker may be asleep or reloading; the next event retries.
      return { error: "BRIDGE_UNAVAILABLE", message: String(error && error.message ? error.message : error) };
    }
  }

  function shouldStop(decision) {
    return (
      !decision ||
      decision.error ||
      !decision.action ||
      decision.action === "none" ||
      decision.action === "halt"
    );
  }

  async function runCycle(events, state) {
    let decision = await toBackground({ type: "AGENT_EVENT", payload: { events, state } });

    for (let step = 0; step < MAX_STEPS_PER_CYCLE; step += 1) {
      if (shouldStop(decision)) {
        if (decision && decision.action === "halt") {
          console.info("[agent] halted:", decision.reason || "no reason given");
        }
        return;
      }
      const result = await globalThis.AgentExecutor.execute(decision);
      if (result.state) publishBridge(result.state, [{ event: "ACTION_RESULT" }]);
      decision = await toBackground({ type: "AGENT_ACTION_RESULT", payload: result });
    }
  }

  async function onObserverEvent({ events, state }) {
    publishBridge(state, events);
    if (busy) {
      pending = { events, state };
      return;
    }
    busy = true;
    try {
      await runCycle(events, state);
    } catch (error) {
      console.warn("[agent] cycle failed:", error);
    } finally {
      busy = false;
      const next = pending;
      pending = null;
      if (next) onObserverEvent(next);
    }
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === "REQUEST_STATE") {
      const state = globalThis.AgentObserver.snapshot("requested");
      publishBridge(state, [{ event: "REQUESTED" }]);
      sendResponse({ state });
      return true;
    }
    if (message?.type === "EXECUTE_ACTION") {
      globalThis.AgentExecutor.execute(message.payload).then(sendResponse);
      return true;
    }
    if (message?.type === "HEARTBEAT") {
      const state = globalThis.AgentObserver.snapshot("heartbeat");
      publishBridge(state, [{ event: "HEARTBEAT" }]);
      sendResponse({ ok: true, state });
      if (state) onObserverEvent({ events: [{ event: "HEARTBEAT" }], state });
      return true;
    }
    if (message?.type === "PING") {
      sendResponse({ ok: true, platform: globalThis.AgentPlatforms.match()?.id || null });
      return true;
    }
    return false;
  });

  function boot() {
    if (!globalThis.AgentPlatforms.match()) return;
    globalThis.AgentObserver.start(onObserverEvent);
    publishBridge(globalThis.AgentObserver.lastState(), [{ event: "STATE_READY" }]);
    console.info("[agent] DOM bridge active for", globalThis.AgentPlatforms.match().id);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();

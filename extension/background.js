"use strict";

// Lifecycle + transport only. The service worker holds no DOM knowledge and
// makes no decision: it forwards normalised state to the local Python agent and
// returns the agent's action to the content script.
const DEFAULT_ENDPOINT = "http://127.0.0.1:8765";
const REQUEST_TIMEOUT_MS = 60000;
const HEALTH_ALARM = "agent-health";

const sessions = new Map(); // tabId -> {session_id, last_seen, offline_until}

async function settings() {
  const stored = await chrome.storage.local.get(["endpoint", "auth_token"]);
  return {
    endpoint: stored.endpoint || DEFAULT_ENDPOINT,
    authToken: stored.auth_token || ""
  };
}

function sessionFor(tabId) {
  let session = sessions.get(tabId);
  if (!session) {
    session = { session_id: `tab-${tabId}-${Date.now().toString(36)}`, offline_until: 0 };
    sessions.set(tabId, session);
  }
  session.last_seen = Date.now();
  return session;
}

async function post(path, body) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const { endpoint, authToken } = await settings();
    const headers = { "Content-Type": "application/json" };
    if (authToken) headers["X-Agent-Token"] = authToken;
    const response = await fetch(`${endpoint}${path}`, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      signal: controller.signal
    });
    if (!response.ok) return { error: "AGENT_HTTP_ERROR", status: response.status };
    return await response.json();
  } catch (error) {
    return { error: "AGENT_UNREACHABLE", message: String(error && error.message ? error.message : error) };
  } finally {
    clearTimeout(timer);
  }
}

async function captureScreenshot(windowId) {
  try {
    return await chrome.tabs.captureVisibleTab(windowId, { format: "png" });
  } catch (error) {
    return null;
  }
}

// The agent may ask for a screenshot when the DOM has no usable evidence.
// That round trip stays inside the worker; the content script only ever
// receives a concrete DOM or coordinate action.
async function resolveDecision(decision, sender, session) {
  let current = decision;
  for (let hop = 0; hop < 3; hop += 1) {
    if (!current || current.action !== "capture_screenshot") return current;
    const image = await captureScreenshot(sender.tab.windowId);
    current = await post("/v1/vision", {
      session_id: session.session_id,
      tab_id: sender.tab.id,
      request_id: current.request_id || null,
      description: current.description || "",
      screenshot: image,
      viewport: current.viewport || null
    });
  }
  return { action: "halt", reason: "vision_fallback_did_not_converge" };
}

async function handle(message, sender) {
  if (!sender.tab) return { error: "NO_TAB" };
  const session = sessionFor(sender.tab.id);
  if (Date.now() < session.offline_until) return { action: "none", reason: "agent_offline_backoff" };

  const kind = message.type === "AGENT_EVENT" ? "event" : "action_result";
  const decision = await post("/v1/decide", {
    session_id: session.session_id,
    tab_id: sender.tab.id,
    url: sender.tab.url || null,
    kind,
    payload: message.payload
  });

  if (decision.error === "AGENT_UNREACHABLE") {
    session.offline_until = Date.now() + 5000;
    return { action: "none", reason: "agent_unreachable" };
  }
  return resolveDecision(decision, sender, session);
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type !== "AGENT_EVENT" && message?.type !== "AGENT_ACTION_RESULT") return false;
  handle(message, sender).then(sendResponse);
  return true;
});

chrome.tabs.onRemoved.addListener((tabId) => sessions.delete(tabId));

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(HEALTH_ALARM, { periodInMinutes: 1 });
});

chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create(HEALTH_ALARM, { periodInMinutes: 1 });
});

// A heartbeat keeps the worker warm and lets the agent nudge an idle tab that
// stopped producing mutations (for example after a long quiet period).
chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== HEALTH_ALARM) return;
  const tabs = await chrome.tabs.query({ url: "https://*.tiktok.com/*" });
  for (const tab of tabs) {
    try {
      await chrome.tabs.sendMessage(tab.id, { type: "HEARTBEAT" });
    } catch (_) {
      // The content script is not ready in this tab yet.
    }
  }
});

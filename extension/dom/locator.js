(() => {
  "use strict";

  // Generic element registry. It turns a live DOM node into a stable target id
  // and can find that node again later, even when TikTok re-renders the tree.
  // Resolution order (strongest evidence first):
  //   1. the live node from the last snapshot (identity + signature match)
  //   2. aria-* attributes
  //   3. data-* attributes
  //   4. visible text inside the same semantic role
  //   5. DOM structural path
  //   6. CSS selector
  //   7. bounding box centre (fallback only)
  const registry = new Map();
  const MAX_TEXT = 160;

  const norm = (value) => String(value || "").replace(/\s+/g, " ").trim();

  function isVisible(node) {
    if (!node || !node.isConnected || typeof node.getBoundingClientRect !== "function") return false;
    const rect = node.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    const style = getComputedStyle(node);
    if (style.visibility === "hidden" || style.display === "none" || Number(style.opacity) === 0) return false;
    return true;
  }

  function inViewport(node) {
    const rect = node.getBoundingClientRect();
    return rect.bottom > 0 && rect.right > 0 && rect.top < innerHeight && rect.left < innerWidth;
  }

  function textOf(node, limit = MAX_TEXT) {
    return norm(node.innerText || node.textContent || node.value || "").slice(0, limit);
  }

  function dataAttributes(node) {
    const out = {};
    for (const item of node.attributes || []) {
      if (item.name.startsWith("data-") && item.value && item.value.length <= 120) {
        out[item.name] = item.value;
      }
    }
    return out;
  }

  function ariaAttributes(node) {
    const out = {};
    for (const item of node.attributes || []) {
      if ((item.name.startsWith("aria-") || item.name === "role") && item.value) {
        out[item.name] = item.value.slice(0, 120);
      }
    }
    return out;
  }

  function structuralPath(node) {
    const parts = [];
    let current = node;
    let depth = 0;
    while (current && current.nodeType === 1 && current !== document.body && depth < 12) {
      const parent = current.parentElement;
      if (!parent) break;
      const tag = current.tagName.toLowerCase();
      const sameTag = [...parent.children].filter((child) => child.tagName === current.tagName);
      const index = sameTag.indexOf(current) + 1;
      parts.unshift(sameTag.length > 1 ? `${tag}:nth-of-type(${index})` : tag);
      current = parent;
      depth += 1;
    }
    return parts.length ? `body ${parts.join(" > ")}`.replace("body ", "body > ") : "body";
  }

  function cssSelector(node) {
    if (node.id && document.querySelectorAll(`#${CSS.escape(node.id)}`).length === 1) {
      return `#${CSS.escape(node.id)}`;
    }
    for (const name of ["data-e2e", "data-testid", "data-conversation-id", "data-message-id"]) {
      const value = node.getAttribute?.(name);
      if (!value) continue;
      const selector = `[${name}="${CSS.escape(value)}"]`;
      if (document.querySelectorAll(selector).length === 1) return selector;
    }
    const label = node.getAttribute?.("aria-label");
    if (label) {
      const selector = `[aria-label="${CSS.escape(label)}"]`;
      if (document.querySelectorAll(selector).length === 1) return selector;
    }
    return structuralPath(node);
  }

  function signature(node, role) {
    return [role, node.tagName?.toLowerCase(), textOf(node, 80), node.getAttribute?.("aria-label") || ""].join("|");
  }

  function rectOf(node) {
    const rect = node.getBoundingClientRect();
    return {
      x: Math.round(rect.left),
      y: Math.round(rect.top),
      width: Math.round(rect.width),
      height: Math.round(rect.height)
    };
  }

  function resourceId(node) {
    // Stable platform-side identity (never a CSS selector): the Python layers
    // match on this, while `selector` stays a selector.
    for (const name of ["data-e2e", "data-testid", "data-conversation-id", "data-message-id"]) {
      const value = node.getAttribute?.(name);
      if (value) return value.slice(0, 200);
    }
    return node.id ? String(node.id).slice(0, 200) : "";
  }

  function describe(node, id, role, extra = {}) {
    const descriptor = {
      id,
      role,
      resource_id: resourceId(node),
      description: (node.getAttribute?.("aria-label") || node.getAttribute?.("title") || "").slice(0, 200),
      href: node.getAttribute?.("href") || "",
      text: extra.text !== undefined ? extra.text : textOf(node),
      aria: ariaAttributes(node),
      data: dataAttributes(node),
      tag: node.tagName.toLowerCase(),
      path: structuralPath(node),
      selector: cssSelector(node),
      rect: rectOf(node),
      visible: isVisible(node),
      enabled: !node.disabled && node.getAttribute?.("aria-disabled") !== "true",
      in_viewport: inViewport(node),
      signature: signature(node, role),
      ...extra
    };
    registry.set(id, { ref: new WeakRef(node), descriptor });
    return descriptor;
  }

  function candidatesForRole(role) {
    const profile = globalThis.AgentPlatforms.match();
    const factory = profile && profile.roles && profile.roles[role];
    try {
      return factory ? factory().filter(Boolean) : [];
    } catch (_) {
      return [];
    }
  }

  function uniqueQuery(selector) {
    try {
      const nodes = [...document.querySelectorAll(selector)].filter(isVisible);
      return nodes.length === 1 ? nodes[0] : null;
    } catch (_) {
      return null;
    }
  }

  // Returns {element, method} or {error}
  function resolve(targetId) {
    const entry = registry.get(targetId);
    if (!entry) return { error: "TARGET_NOT_REGISTERED" };
    const { descriptor } = entry;

    const live = entry.ref.deref();
    if (live && live.isConnected && signature(live, descriptor.role) === descriptor.signature) {
      return { element: live, method: "live_node", descriptor };
    }

    for (const [name, value] of Object.entries(descriptor.aria)) {
      if (name === "role") continue;
      const found = uniqueQuery(`[${name}="${CSS.escape(value)}"]`);
      if (found) return { element: found, method: `aria:${name}`, descriptor };
    }

    for (const [name, value] of Object.entries(descriptor.data)) {
      const found = uniqueQuery(`[${name}="${CSS.escape(value)}"]`);
      if (found) return { element: found, method: `data:${name}`, descriptor };
    }

    if (descriptor.text) {
      const byText = candidatesForRole(descriptor.role).find(
        (node) => isVisible(node) && textOf(node).startsWith(descriptor.text.slice(0, 40))
      );
      if (byText) return { element: byText, method: "role+text", descriptor };
    }

    const byPath = uniqueQuery(descriptor.path);
    if (byPath) return { element: byPath, method: "structure", descriptor };

    const bySelector = uniqueQuery(descriptor.selector);
    if (bySelector) return { element: bySelector, method: "selector", descriptor };

    const roleCandidates = candidatesForRole(descriptor.role).filter(isVisible);
    if (roleCandidates.length === 1) {
      return { element: roleCandidates[0], method: "role_singleton", descriptor };
    }

    const rect = descriptor.rect;
    if (rect && rect.width > 0) {
      const hit = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
      const matched = hit && roleCandidates.find((node) => node === hit || node.contains(hit));
      if (matched) return { element: matched, method: "bbox", descriptor };
    }

    return { error: "TARGET_NOT_FOUND", descriptor };
  }

  function reset() {
    registry.clear();
  }

  function descriptorOf(targetId) {
    return registry.get(targetId)?.descriptor || null;
  }

  globalThis.AgentLocator = Object.freeze({
    describe,
    resolve,
    reset,
    descriptorOf,
    isVisible,
    inViewport,
    textOf,
    rectOf,
    resourceId,
    signature,
    size: () => registry.size
  });
})();

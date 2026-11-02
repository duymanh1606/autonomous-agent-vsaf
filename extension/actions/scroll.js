(() => {
  "use strict";

  // Scrolling never navigates. It only brings existing DOM into view so the
  // extractor can read it and the executor can click it.
  const frame = () => new Promise((resolve) => requestAnimationFrame(() => resolve()));

  function scrollableAncestor(element) {
    let current = element;
    while (current && current !== document.body) {
      const style = getComputedStyle(current);
      const scrollable = /(auto|scroll|overlay)/.test(`${style.overflowY}${style.overflow}`);
      if (scrollable && current.scrollHeight > current.clientHeight + 4) return current;
      current = current.parentElement;
    }
    return document.scrollingElement || document.documentElement;
  }

  async function scrollTo(targetId, { block = "center" } = {}) {
    const resolved = globalThis.AgentLocator.resolve(targetId);
    if (resolved.error) return { ok: false, error: resolved.error, target: targetId };
    resolved.element.scrollIntoView({ block, inline: "nearest" });
    await frame();
    await frame();
    return { ok: true, target: targetId, method: resolved.method, rect: globalThis.AgentLocator.rectOf(resolved.element) };
  }

  async function scrollBy(targetId, delta = 400) {
    const resolved = targetId ? globalThis.AgentLocator.resolve(targetId) : null;
    if (resolved && resolved.error) return { ok: false, error: resolved.error, target: targetId };
    const container = resolved ? scrollableAncestor(resolved.element) : document.scrollingElement;
    const before = container.scrollTop;
    container.scrollBy({ top: Number(delta) || 0, behavior: "auto" });
    await frame();
    return { ok: true, target: targetId || null, from: before, to: container.scrollTop };
  }

  async function scrollToLatest(targetId) {
    const resolved = targetId ? globalThis.AgentLocator.resolve(targetId) : null;
    if (resolved && resolved.error) return { ok: false, error: resolved.error, target: targetId };
    const container = resolved ? scrollableAncestor(resolved.element) : document.scrollingElement;
    container.scrollTop = container.scrollHeight;
    await frame();
    return { ok: true, target: targetId || null, position: container.scrollTop };
  }

  globalThis.AgentScroll = Object.freeze({ scrollTo, scrollBy, scrollToLatest, scrollableAncestor });
})();

(() => {
  "use strict";

  // Clicking is DOM-first: the element is resolved, checked, scrolled into
  // view and then clicked directly. Coordinates are computed only to verify the
  // element is really reachable, and are used as a click input just for the
  // vision fallback path.
  const frame = () => new Promise((resolve) => requestAnimationFrame(() => resolve()));

  function centreOf(element) {
    const rect = element.getBoundingClientRect();
    return {
      x: Math.round(rect.left + rect.width / 2),
      y: Math.round(rect.top + rect.height / 2),
      rect: {
        x: Math.round(rect.left),
        y: Math.round(rect.top),
        width: Math.round(rect.width),
        height: Math.round(rect.height)
      }
    };
  }

  function dispatchPointer(element, point) {
    const base = { bubbles: true, cancelable: true, composed: true, clientX: point.x, clientY: point.y, button: 0 };
    element.dispatchEvent(new PointerEvent("pointerdown", { ...base, pointerId: 1, isPrimary: true }));
    element.dispatchEvent(new MouseEvent("mousedown", base));
    element.dispatchEvent(new PointerEvent("pointerup", { ...base, pointerId: 1, isPrimary: true }));
    element.dispatchEvent(new MouseEvent("mouseup", base));
  }

  async function click(targetId) {
    const locator = globalThis.AgentLocator;
    const resolved = locator.resolve(targetId);
    if (resolved.error) return { ok: false, error: resolved.error, target: targetId };

    const element = resolved.element;
    if (!locator.isVisible(element)) {
      return { ok: false, error: "TARGET_NOT_VISIBLE", target: targetId, method: resolved.method };
    }

    if (!locator.inViewport(element)) {
      element.scrollIntoView({ block: "center", inline: "nearest" });
      await frame();
      await frame();
    }
    if (!locator.isVisible(element)) {
      return { ok: false, error: "TARGET_NOT_VISIBLE", target: targetId, method: resolved.method };
    }

    const point = centreOf(element);
    const hit = document.elementFromPoint(point.x, point.y);
    if (hit && hit !== element && !element.contains(hit) && !hit.contains(element)) {
      return { ok: false, error: "TARGET_OCCLUDED", target: targetId, point, method: resolved.method };
    }

    dispatchPointer(element, point);
    if (typeof element.click === "function") element.click();
    await frame();
    return { ok: true, target: targetId, method: resolved.method, point, rect: point.rect };
  }

  // Vision fallback only: click whatever element sits under a coordinate.
  async function clickAt(x, y) {
    const element = document.elementFromPoint(Math.round(x), Math.round(y));
    if (!element) return { ok: false, error: "NO_ELEMENT_AT_POINT", point: { x, y } };
    const clickable =
      element.closest('a,button,[role="button"],[role="link"],[role="listitem"],[tabindex]') || element;
    dispatchPointer(clickable, { x: Math.round(x), y: Math.round(y) });
    if (typeof clickable.click === "function") clickable.click();
    await frame();
    return {
      ok: true,
      point: { x: Math.round(x), y: Math.round(y) },
      method: "coordinate",
      tag: clickable.tagName.toLowerCase()
    };
  }

  globalThis.AgentClick = Object.freeze({ click, clickAt, centreOf, frame });
})();

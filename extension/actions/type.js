(() => {
  "use strict";

  // Text entry for both <textarea>/<input> and contenteditable composers.
  // The result always reports what the field really contains afterwards, so the
  // agent verifies the typed text instead of trusting the action.
  const frame = () => new Promise((resolve) => requestAnimationFrame(() => resolve()));

  function readValue(element) {
    return element.isContentEditable
      ? (element.innerText || element.textContent || "").trim()
      : String(element.value || "").trim();
  }

  function setNativeValue(element, value) {
    const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement : HTMLInputElement;
    const setter = Object.getOwnPropertyDescriptor(prototype.prototype, "value")?.set;
    if (setter) setter.call(element, value);
    else element.value = value;
    element.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function clearContentEditable(element) {
    const selection = getSelection();
    const range = document.createRange();
    range.selectNodeContents(element);
    selection.removeAllRanges();
    selection.addRange(range);
    document.execCommand("delete", false, null);
  }

  function insertText(element, text) {
    element.dispatchEvent(
      new InputEvent("beforeinput", { bubbles: true, cancelable: true, inputType: "insertText", data: text })
    );
    const inserted = document.execCommand("insertText", false, text);
    if (!inserted) {
      element.textContent = text;
      element.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
    }
  }

  async function type(targetId, text, { clear = true } = {}) {
    const locator = globalThis.AgentLocator;
    const resolved = locator.resolve(targetId);
    if (resolved.error) return { ok: false, error: resolved.error, target: targetId };

    const element = resolved.element;
    if (!locator.isVisible(element)) return { ok: false, error: "TARGET_NOT_VISIBLE", target: targetId };
    if (element.disabled || element.getAttribute("aria-disabled") === "true") {
      return { ok: false, error: "TARGET_DISABLED", target: targetId };
    }

    if (!locator.inViewport(element)) {
      element.scrollIntoView({ block: "center" });
      await frame();
    }
    element.focus({ preventScroll: true });
    if (document.activeElement !== element && element.isContentEditable) {
      const selection = getSelection();
      const range = document.createRange();
      range.selectNodeContents(element);
      range.collapse(false);
      selection.removeAllRanges();
      selection.addRange(range);
    }

    const value = String(text ?? "");
    if (element.isContentEditable) {
      if (clear) clearContentEditable(element);
      insertText(element, value);
    } else {
      setNativeValue(element, clear ? value : `${element.value || ""}${value}`);
    }
    await frame();

    const actual = readValue(element);
    return {
      ok: actual === value.trim(),
      error: actual === value.trim() ? null : "TEXT_MISMATCH",
      target: targetId,
      method: resolved.method,
      expected: value.trim(),
      actual
    };
  }

  async function pressEnter(targetId) {
    const resolved = globalThis.AgentLocator.resolve(targetId);
    if (resolved.error) return { ok: false, error: resolved.error, target: targetId };
    const element = resolved.element;
    element.focus({ preventScroll: true });
    for (const kind of ["keydown", "keypress", "keyup"]) {
      element.dispatchEvent(
        new KeyboardEvent(kind, { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true })
      );
    }
    await frame();
    return { ok: true, target: targetId, method: "enter_key" };
  }

  globalThis.AgentType = Object.freeze({ type, pressEnter, readValue });
})();

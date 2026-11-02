(() => {
  "use strict";

  // File attachment for chat composers.
  //
  // The page cannot read the operator's disk, and the content script cannot
  // either. The bytes therefore arrive from the local agent: the action carries
  // a URL served by the agent's own loopback API, this fetches it and hands the
  // page a real `File` through a `DataTransfer`, which is the only way to put a
  // file into an `<input type="file">` that the site's own JavaScript accepts.
  //
  // Like every other action here it reports what the DOM actually shows
  // afterwards; whether the attachment counts as "sent" is decided by the agent
  // re-reading the thread, never by this returning ok.
  const MAX_BYTES = 25 * 1024 * 1024;
  const frame = () => new Promise((resolve) => requestAnimationFrame(() => resolve()));

  function fileInputFor(targetId) {
    // A visible attach button usually hides its input elsewhere in the form, so
    // the input is searched for globally when the target does not hold one.
    if (targetId) {
      const resolved = globalThis.AgentLocator.resolve(targetId);
      if (!resolved.error && resolved.element) {
        const owned = resolved.element.querySelector?.('input[type="file"]');
        if (owned) return owned;
        if (resolved.element.tagName === "INPUT" && resolved.element.type === "file") {
          return resolved.element;
        }
      }
    }
    const candidates = Array.from(document.querySelectorAll('input[type="file"]'));
    return candidates.find((input) => !input.disabled) || null;
  }

  async function download(url) {
    const response = await fetch(url, { credentials: "omit" });
    if (!response.ok) {
      return { error: "ATTACHMENT_FETCH_FAILED", detail: String(response.status) };
    }
    const blob = await response.blob();
    if (blob.size > MAX_BYTES) {
      return { error: "ATTACHMENT_TOO_LARGE", detail: String(blob.size) };
    }
    return { blob };
  }

  async function attach(targetId, url, name, mime) {
    if (!url) return { ok: false, error: "ATTACHMENT_URL_MISSING" };

    const input = fileInputFor(targetId);
    if (!input) return { ok: false, error: "FILE_INPUT_NOT_FOUND" };

    const fetched = await download(url);
    if (fetched.error) {
      return { ok: false, error: fetched.error, detail: fetched.detail };
    }

    const file = new File(
      [fetched.blob],
      name || "attachment",
      { type: mime || fetched.blob.type || "application/octet-stream" }
    );

    const transfer = new DataTransfer();
    transfer.items.add(file);
    input.files = transfer.files;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    await frame();

    // The site clears `input.files` once it has taken the attachment, so both
    // "still attached" and "consumed" are success; only an untouched empty
    // input means nothing happened.
    return {
      ok: true,
      target: targetId || null,
      file: { name: file.name, size: file.size, type: file.type },
      attached_count: input.files ? input.files.length : 0
    };
  }

  globalThis.AgentAttach = Object.freeze({ attach });
})();

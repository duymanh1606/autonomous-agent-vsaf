/**
 * The noVNC URL the dashboard opens.
 *
 * This used to be `http://<hostname>:<novnc_port>/vnc.html`, which only
 * worked when the dashboard was itself on loopback: websockify binds
 * 127.0.0.1 and nothing else, so reaching the dashboard over a public IP or
 * a tailnet produced a URL to a port that refuses every connection. It is
 * now same-origin and relative, so nginx makes the loopback hop on the
 * server - after the supervisor has checked this user may see this
 * workspace (see tests/test_vnc_auth.py).
 */
import { describe, expect, it, vi } from "vitest";

import { openVncWindow } from "./api";

function urlFor(id: string): string {
  const open = vi.fn();
  vi.stubGlobal("open", open);
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => ({}) })));
  openVncWindow({ id });
  return String(open.mock.calls[0][0]);
}

describe("the VNC url", () => {
  it("is relative, so it inherits the dashboard's own origin", () => {
    const url = urlFor("shop-abc");
    // No scheme and no host: whatever fronts the dashboard - an SSH tunnel
    // today, a Cloudflare hostname later - carries this unchanged.
    expect(url.startsWith("/vnc/shop-abc/")).toBe(true);
    expect(url).not.toContain("http://");
    expect(url).not.toContain("26081");
  });

  it("names the websocket path explicitly", () => {
    // Not optional: noVNC builds its socket URL as host + "/" + path,
    // anchored at the root rather than relative to the page. Without this
    // the page loads and then hangs on a black screen hitting /websockify.
    const url = urlFor("shop-abc");
    expect(url).toContain("path=vnc/shop-abc/websockify");
    expect(url).toContain("autoconnect=true");
  });

  it("escapes the workspace id", () => {
    // Ids are generated as `shop-<hex>`, but this is the one place a name
    // reaches a URL - an id that ever carries a slash must not be able to
    // rewrite the path it lands on.
    expect(urlFor("a/b")).toContain("/vnc/a%2Fb/");
  });
});

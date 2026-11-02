// crypto.randomUUID only exists in a secure context (HTTPS or localhost). The
// dashboard is also opened over plain HTTP on a LAN address, where calling it
// throws - and the composer's Send then did nothing at all: no request, no
// bubble, the text left sitting in the box. crypto.getRandomValues has no such
// restriction, so fall back to building the v4 UUID from it.
export function newClientId(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80; // RFC 4122 variant
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

// A new catalogue product's id: short enough to read out loud, random so
// nobody has to invent one. Only [a-z0-9-], which is what the products API
// accepts (dashboard_api/main.py:create_product).
export function newProductId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(4));
  return `sp-${Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}

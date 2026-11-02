import { newClientId } from "./id";

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

describe("newClientId", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("still works over plain HTTP, where crypto.randomUUID is missing", () => {
    vi.stubGlobal("crypto", {
      getRandomValues: globalThis.crypto.getRandomValues.bind(globalThis.crypto),
    });

    const first = newClientId();
    expect(first).toMatch(UUID_V4);
    expect(newClientId()).not.toBe(first);
  });

  it("uses crypto.randomUUID when the browser has it", () => {
    vi.stubGlobal("crypto", { ...globalThis.crypto, randomUUID: () => "from-browser" });
    expect(newClientId()).toBe("from-browser");
  });
});

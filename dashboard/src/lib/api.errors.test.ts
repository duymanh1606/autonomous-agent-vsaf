/**
 * What a failed API call reads like to the operator.
 *
 * Before `ApiError`, the raw response was the message: `401: {"detail":"sai
 * email hoặc mật khẩu"}`, a FastAPI validation list cut off mid-JSON, or the
 * browser's bare `Failed to fetch`. These pin the translation into sentences.
 */
import { ApiError, api } from "./api";

function respond(status: number, body: string) {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(body, {
    status, headers: { "Content-Type": "application/json" },
  })));
}

async function failing(): Promise<ApiError> {
  try {
    await api.accounts();
  } catch (error) {
    return error as ApiError;
  }
  throw new Error("expected the call to fail");
}

describe("API errors as the operator sees them", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("uses the server's own sentence when it gave one, with no status or JSON", async () => {
    respond(400, JSON.stringify({ detail: "combo cần ít nhất một sản phẩm lẻ" }));
    const error = await failing();
    expect(error).toBeInstanceOf(ApiError);
    expect(error.message).toBe("combo cần ít nhất một sản phẩm lẻ");
    expect(error.message).not.toMatch(/400|detail|\{/);
    expect(error.status).toBe(400);
  });

  it("turns a FastAPI validation list into field: reason, not truncated JSON", async () => {
    respond(422, JSON.stringify({ detail: [
      { type: "string_type", loc: ["body", "email"], msg: "Input should be a valid string", input: 123 },
      { type: "missing", loc: ["body", "password"], msg: "Field required", input: null },
    ] }));
    const error = await failing();
    expect(error.message).toBe("email: Input should be a valid string; password: Field required");
  });

  it("says a session has expired for 401 instead of echoing the code path", async () => {
    respond(401, JSON.stringify({ detail: "not authenticated" }));
    const error = await failing();
    expect(error.status).toBe(401);
    expect(error.message).toMatch(/đăng nhập/);
    expect(error.message).not.toMatch(/not authenticated/);
    // The technical detail is still there for anything that wants it.
    expect(error.detail).toBe("not authenticated");
  });

  it("keeps a specific 401 like a wrong password as written", async () => {
    // Login also answers 401 - but with the reason. Overriding that with
    // "your session expired" would tell someone who mistyped their password
    // something untrue.
    respond(401, JSON.stringify({ detail: "sai tài khoản hoặc mật khẩu" }));
    const error = await failing();
    expect(error.message).toBe("sai tài khoản hoặc mật khẩu");
  });

  it("explains a server failure without exposing an HTML error page", async () => {
    respond(502, "<html><body>Bad Gateway</body></html>");
    const error = await failing();
    expect(error.message).toMatch(/Máy chủ gặp lỗi/);
    expect(error.message).not.toMatch(/<html>/);
  });

  it("explains a request that never got an answer", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch"); }));
    const error = await failing();
    expect(error.status).toBe(0);
    expect(error.message).toMatch(/Không kết nối được/);
    expect(error.message).not.toMatch(/Failed to fetch/);
  });

  it("is still a real Error for code that only knows about Error", async () => {
    respond(404, JSON.stringify({ detail: "unknown product" }));
    const error = await failing();
    expect(error).toBeInstanceOf(Error);
    expect(error.name).toBe("ApiError");
  });
});

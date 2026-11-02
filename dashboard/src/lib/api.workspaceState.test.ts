/**
 * `workspaceState` is what every Start/Stop control keys on. It used to see
 * only the agent process, so a Chrome left running by an agent that crashed
 * at boot (no GEMINI_API_KEY is the usual cause) read as "idle": the UI
 * offered Start instead of Stop and there was no way to close that Chrome.
 */
import { WORKSPACE_STATE_LABEL, workspaceState } from "./api";
import type { Account } from "./api";

const account = (chrome: boolean, agent: boolean, extra: Partial<Account> = {}) => ({
  lifecycle_state: "active", control_connected: false,
  processes: { chrome: { running: chrome, pid: chrome ? 1 : null }, agent: { running: agent, pid: agent ? 2 : null } },
  ...extra,
}) as unknown as Account;

describe("workspaceState", () => {
  it("reports a Chrome without its agent distinctly, never as idle", () => {
    expect(workspaceState(account(true, false), undefined)).toBe("chrome_only");
    expect(WORKSPACE_STATE_LABEL.chrome_only).toMatch(/Chrome đang mở/);
  });

  it("is idle only when nothing at all is running", () => {
    expect(workspaceState(account(false, false), undefined)).toBe("idle");
  });

  it("still reports a running agent as running", () => {
    expect(workspaceState(account(true, true, { control_connected: true }), {
      paused: false, security_challenge: false, sessions: [],
    } as any)).toBe("running");
  });
});

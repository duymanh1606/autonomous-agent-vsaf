import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState,
  type ReactNode,
} from "react";
import { CheckCircleIcon, XCircleIcon, XIcon } from "./ui/icons";

/**
 * Transient feedback that finds the operator wherever they are looking.
 *
 * Errors used to render as a red line at a fixed spot inside the page - fine
 * if you happen to be looking there, invisible if you are three rows down in
 * the conversation list. A toast stacks in the corner, announces itself to
 * assistive tech, clears itself, and never blocks the thing you were doing.
 */
type Kind = "error" | "success" | "info";

export type Toast = { id: number; kind: Kind; title: string; detail?: string };

type Api = {
  push: (toast: Omit<Toast, "id">) => number;
  dismiss: (id: number) => void;
  error: (title: string, detail?: string) => number;
  success: (title: string, detail?: string) => number;
};

const ToastContext = createContext<Api | null>(null);

// Errors stay until read - the operator has to know a send failed. Successes
// are confirmations of something they just watched happen, so they clear.
const AUTO_DISMISS_MS: Record<Kind, number | null> = { error: null, success: 4000, info: 6000 };

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);
  const timers = useRef(new Map<number, ReturnType<typeof setTimeout>>());

  const dismiss = useCallback((id: number) => {
    const timer = timers.current.get(id);
    if (timer) clearTimeout(timer);
    timers.current.delete(id);
    setToasts((prev) => prev.filter((item) => item.id !== id));
  }, []);

  const push = useCallback((toast: Omit<Toast, "id">) => {
    const id = nextId.current++;
    setToasts((prev) => [...prev, { ...toast, id }]);
    const ttl = AUTO_DISMISS_MS[toast.kind];
    if (ttl !== null) timers.current.set(id, setTimeout(() => dismiss(id), ttl));
    return id;
  }, [dismiss]);

  useEffect(() => () => timers.current.forEach(clearTimeout), []);

  const api = useMemo<Api>(() => ({
    push,
    dismiss,
    error: (title, detail) => push({ kind: "error", title, detail }),
    success: (title, detail) => push({ kind: "success", title, detail }),
  }), [push, dismiss]);

  return (
    <ToastContext.Provider value={api}>
      {children}
      {/* `aria-live` is what makes a screen reader read a new toast without
          the operator having to find it. "assertive" for errors: those should
          interrupt, a success can wait for the current sentence to finish. */}
      <div className="toast-stack">
        <div aria-live="assertive" aria-atomic="true" className="sr-only">
          {toasts.filter((t) => t.kind === "error").map((t) => t.title).join(". ")}
        </div>
        <div aria-live="polite" aria-atomic="true" className="sr-only">
          {toasts.filter((t) => t.kind !== "error").map((t) => t.title).join(". ")}
        </div>
        {toasts.map((toast) => (
          <div key={toast.id} className={`toast toast-${toast.kind}`} role={toast.kind === "error" ? "alert" : "status"}>
            <span className="toast-icon">
              {toast.kind === "error"
                ? <XCircleIcon width={16} height={16} />
                : <CheckCircleIcon width={16} height={16} />}
            </span>
            <span className="toast-body">
              <span className="toast-title">{toast.title}</span>
              {toast.detail && <span className="toast-detail">{toast.detail}</span>}
            </span>
            <button className="toast-close" aria-label="Đóng thông báo" onClick={() => dismiss(toast.id)}>
              <XIcon width={14} height={14} />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): Api {
  const api = useContext(ToastContext);
  if (!api) throw new Error("useToast must be used inside <ToastProvider>");
  return api;
}

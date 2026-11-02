import { Component, type ErrorInfo, type ReactNode } from "react";

/**
 * Keeps one broken region from taking the whole dashboard down.
 *
 * Without a boundary, any render error anywhere - a null the API started
 * returning, a date that failed to parse - unmounts the entire React tree and
 * leaves the operator staring at a blank page with no way back but a reload.
 * With one around each page, the sidebar and every other page keep working,
 * and the broken region says what happened and offers to try again.
 *
 * Deliberately a class: React only exposes error boundaries through
 * `componentDidCatch`/`getDerivedStateFromError`, there is no hook form.
 */
type Props = {
  children: ReactNode;
  /** Names the region in the fallback, so "Products crashed" reads as such. */
  label?: string;
  /** Re-mounts the children with a fresh boundary state when it changes -
   * navigating to another page must not keep showing the old page's crash. */
  resetKey?: unknown;
  onError?: (error: Error, info: ErrorInfo) => void;
};

type State = { error: Error | null };

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    this.props.onError?.(error, info);
    // Always to the console as well: the fallback is for the operator, the
    // stack is for whoever has to fix it.
    console.error("[ui] render error in", this.props.label ?? "region", error, info.componentStack);
  }

  componentDidUpdate(prev: Props) {
    if (this.state.error && prev.resetKey !== this.props.resetKey) {
      this.setState({ error: null });
    }
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="error-boundary" role="alert">
        <div className="error-boundary-title">
          {this.props.label ? `${this.props.label} gặp lỗi hiển thị` : "Khu vực này gặp lỗi hiển thị"}
        </div>
        <p className="error-boundary-detail">
          Phần còn lại của trang vẫn hoạt động. Thử lại, hoặc chuyển sang mục
          khác rồi quay lại.
        </p>
        <code className="error-boundary-message">{this.state.error.message}</code>
        <button className="primary" onClick={() => this.setState({ error: null })}>
          Thử lại
        </button>
      </div>
    );
  }
}

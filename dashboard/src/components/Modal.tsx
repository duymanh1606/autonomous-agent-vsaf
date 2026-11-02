import { useEffect, useId, useRef, type CSSProperties, type ReactNode } from "react";
import { createPortal } from "react-dom";

/**
 * The one dialog behaviour every modal in the dashboard shares.
 *
 * Six modals used to hand-roll the same overlay + stopPropagation pair, and
 * none of them told assistive technology a dialog had opened, kept keyboard
 * focus inside, closed on Escape, or put focus back where it came from. Those
 * are the four things WCAG asks of a dialog, and they are the same four for
 * every modal, so they live here once.
 *
 * Visuals are the caller's: `className`/`style` go straight onto the panel,
 * so a modal keeps whatever look it already had.
 */
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
  'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function Modal({
  onClose, title, children, className = "product-form-modal", overlayClassName = "product-form-overlay", style,
}: {
  onClose: () => void;
  /** Rendered as the dialog's accessible name. Pass the visible heading text. */
  title: string;
  children: ReactNode;
  className?: string;
  overlayClassName?: string;
  style?: CSSProperties;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const titleId = useId();
  // Callers routinely pass an inline `() => ...` for onClose, so its identity
  // changes on every render of the caller - including ones unrelated to this
  // dialog, like a background refetch ticking a parent's query. A ref keeps
  // the mount effect below from treating that as "the dialog changed" and
  // re-running its initial-focus step, which would otherwise yank focus back
  // to the first control (see OrderFormModal: the header's "Đóng" button
  // sits before the form fields) while someone is mid-keystroke.
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    // First focusable control, else the panel itself - a dialog nobody can
    // reach by keyboard is one a keyboard user cannot close.
    const first = panel.current?.querySelector<HTMLElement>(FOCUSABLE);
    // preventScroll: this element is nested wherever the caller put <Modal>
    // in the tree - usually inside a scrolled list (Products, Người dùng...),
    // not portalled to <body>. The overlay is position: fixed and always
    // centres in the viewport regardless, but without this option a plain
    // .focus() still walks up and scrolls that *ancestor* list back to the
    // element's un-fixed layout position - confirmed live: open "Sửa" while
    // scrolled down the product grid and the whole page snapped to the top.
    (first ?? panel.current)?.focus({ preventScroll: true });

    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab" || !panel.current) return;
      const items = Array.from(panel.current.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (!items.length) { event.preventDefault(); return; }
      const [head, tail] = [items[0], items[items.length - 1]];
      // Wrap at both ends so Tab can never leave the dialog for the page
      // behind it, which is still there but must be inert while this is up.
      if (event.shiftKey && document.activeElement === head) {
        event.preventDefault(); tail.focus({ preventScroll: true });
      } else if (!event.shiftKey && document.activeElement === tail) {
        event.preventDefault(); head.focus({ preventScroll: true });
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      // Back to the control that opened it, so closing does not dump focus
      // at the top of the document.
      opener?.focus?.({ preventScroll: true });
    };
  }, []);

  // Portalled to <body>, not rendered inline where JSX put <Modal> (usually
  // deep inside a scrolled list - Products, Người dùng...). Two real bugs
  // came from *not* doing this: a long list's own overflow could clip a
  // modal at its scrolled edge, and - confirmed live - inserting the panel
  // as a DOM descendant of that scrolled container let the browser's own
  // focus-into-view/scroll-anchoring behaviour yank the list back to the
  // panel's un-fixed layout position the instant it opened, even though the
  // panel itself renders position: fixed and centred regardless of where in
  // the tree it lives.
  return createPortal(
    <div className={overlayClassName} onClick={onClose}>
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className={className}
        style={style}
        onClick={(event) => event.stopPropagation()}
      >
        <span id={titleId} className="sr-only">{title}</span>
        {children}
      </div>
    </div>,
    document.body,
  );
}

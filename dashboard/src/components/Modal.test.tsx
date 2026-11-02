import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Modal } from "./Modal";

function Harness({ onClose }: { onClose: () => void }) {
  return (
    <>
      <button>ngoài modal</button>
      <Modal onClose={onClose} title="Sửa sản phẩm">
        <input placeholder="tên" />
        <button>Huỷ</button>
        <button>Lưu</button>
      </Modal>
    </>
  );
}

describe("Modal", () => {
  it("is announced as a dialog named by its title", () => {
    render(<Harness onClose={() => {}} />);
    const dialog = screen.getByRole("dialog", { name: "Sửa sản phẩm" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
  });

  it("moves focus inside when it opens", () => {
    render(<Harness onClose={() => {}} />);
    expect(screen.getByPlaceholderText("tên")).toHaveFocus();
  });

  it("closes on Escape", async () => {
    const onClose = vi.fn();
    render(<Harness onClose={onClose} />);
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("keeps Tab inside the dialog in both directions", async () => {
    render(<Harness onClose={() => {}} />);
    const input = screen.getByPlaceholderText("tên");
    const save = screen.getByRole("button", { name: "Lưu" });

    save.focus();
    await userEvent.tab();
    // Off the end wraps to the first control - never to "ngoài modal".
    expect(input).toHaveFocus();

    await userEvent.tab({ shift: true });
    expect(save).toHaveFocus();
  });

  it("closes when the backdrop is clicked but not the panel", async () => {
    const onClose = vi.fn();
    render(<Harness onClose={onClose} />);
    await userEvent.click(screen.getByRole("dialog"));
    expect(onClose).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("dialog").parentElement!);
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("does not steal focus back to the first control when a caller re-renders with a fresh onClose", async () => {
    // Regression test: OrderFormModal (dashboard/src/routes/Orders.tsx) puts
    // its "Đóng" close button before the form fields in the DOM, and passes
    // `onClose={() => setFormOpen(null)}` - a new function identity on every
    // render of its parent, including ones unrelated to this dialog, like the
    // orders list's 15s background refetch. If the mount effect ever re-ran
    // on that identity change, it would call first-focusable-wins again and
    // yank focus off whatever field the operator is mid-keystroke in back
    // onto that earlier close button.
    function HeaderFirst({ onClose }: { onClose: () => void }) {
      return (
        <Modal onClose={onClose} title="Sửa sản phẩm">
          <button>Đóng</button>
          <input placeholder="tên" />
        </Modal>
      );
    }

    const { rerender } = render(<HeaderFirst onClose={() => {}} />);
    const input = screen.getByPlaceholderText("tên");
    const closeButton = screen.getByRole("button", { name: "Đóng" });
    // Confirms the setup actually matches OrderFormModal's DOM order - the
    // close button really is first, so the assertion below is meaningful.
    expect(closeButton).toHaveFocus();

    input.focus();
    await userEvent.type(input, "a");
    // A fresh onClose identity, as an inline arrow function would produce on
    // every parent render - with no click or key event involved.
    rerender(<HeaderFirst onClose={() => {}} />);

    expect(input).toHaveFocus();
  });

  it("returns focus to the control that opened it", () => {
    const opener = document.createElement("button");
    opener.textContent = "mở";
    document.body.appendChild(opener);
    opener.focus();
    expect(opener).toHaveFocus();

    const { unmount } = render(<Modal onClose={() => {}} title="x"><button>a</button></Modal>);
    expect(opener).not.toHaveFocus();
    unmount();
    expect(opener).toHaveFocus();
    opener.remove();
  });
});

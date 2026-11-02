import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ToastProvider, useToast } from "./Toast";

function Trigger() {
  const toast = useToast();
  return (
    <>
      <button onClick={() => toast.error("Gửi tin thất bại", "agent không phản hồi")}>lỗi</button>
      <button onClick={() => toast.success("Đã lưu")}>xong</button>
    </>
  );
}

const renderWithToasts = () => render(<ToastProvider><Trigger /></ToastProvider>);

describe("Toast", () => {
  it("shows an error as an alert with its detail", async () => {
    renderWithToasts();
    await userEvent.click(screen.getByText("lỗi"));
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Gửi tin thất bại");
    expect(alert).toHaveTextContent("agent không phản hồi");
  });

  it("keeps an error until the operator dismisses it", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderWithToasts();
    await userEvent.click(screen.getByText("lỗi"));
    act(() => { vi.advanceTimersByTime(60_000); });
    // Still there: a failed send is something the operator must not miss.
    expect(screen.getByRole("alert")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Đóng thông báo" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    vi.useRealTimers();
  });

  it("clears a success on its own", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderWithToasts();
    await userEvent.click(screen.getByText("xong"));
    expect(screen.getByRole("status")).toHaveTextContent("Đã lưu");

    act(() => { vi.advanceTimersByTime(4500); });
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    vi.useRealTimers();
  });

  it("announces errors to assistive technology", async () => {
    renderWithToasts();
    await userEvent.click(screen.getByText("lỗi"));
    const live = document.querySelector('[aria-live="assertive"]');
    expect(live).toHaveTextContent("Gửi tin thất bại");
  });

  it("stacks several toasts instead of replacing the previous one", async () => {
    renderWithToasts();
    await userEvent.click(screen.getByText("lỗi"));
    await userEvent.click(screen.getByText("lỗi"));
    expect(screen.getAllByRole("alert")).toHaveLength(2);
  });

  it("refuses to be used outside its provider, loudly", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => render(<Trigger />)).toThrow(/ToastProvider/);
  });
});

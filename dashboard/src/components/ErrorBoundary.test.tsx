import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ErrorBoundary } from "./ErrorBoundary";

function Bomb({ explode }: { explode: boolean }) {
  if (explode) throw new Error("dữ liệu hỏng");
  return <p>nội dung bình thường</p>;
}

describe("ErrorBoundary", () => {
  // React logs every caught render error to console.error; that noise is
  // expected here and would otherwise bury the assertions.
  beforeEach(() => vi.spyOn(console, "error").mockImplementation(() => {}));

  it("renders its children while nothing is wrong", () => {
    render(<ErrorBoundary><Bomb explode={false} /></ErrorBoundary>);
    expect(screen.getByText("nội dung bình thường")).toBeInTheDocument();
  });

  it("contains a render error instead of unmounting everything around it", () => {
    render(
      <>
        <p>sidebar vẫn còn</p>
        <ErrorBoundary label="Products"><Bomb explode={true} /></ErrorBoundary>
      </>,
    );
    expect(screen.getByText("sidebar vẫn còn")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Products gặp lỗi hiển thị");
    expect(screen.getByText("dữ liệu hỏng")).toBeInTheDocument();
  });

  it("reports the error so it can be logged or sent somewhere", () => {
    const onError = vi.fn();
    render(<ErrorBoundary onError={onError}><Bomb explode={true} /></ErrorBoundary>);
    expect(onError).toHaveBeenCalledOnce();
    expect(onError.mock.calls[0][0]).toBeInstanceOf(Error);
  });

  it("recovers when the operator presses retry", async () => {
    let explode = true;
    function Flaky() { return <Bomb explode={explode} />; }
    render(<ErrorBoundary><Flaky /></ErrorBoundary>);
    expect(screen.getByRole("alert")).toBeInTheDocument();

    explode = false;
    await userEvent.click(screen.getByRole("button", { name: "Thử lại" }));

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("nội dung bình thường")).toBeInTheDocument();
  });

  it("clears a crash when the region it wraps changes", () => {
    const { rerender } = render(
      <ErrorBoundary resetKey="products"><Bomb explode={true} /></ErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();

    // Navigating to another page must not keep showing the old page's crash.
    rerender(<ErrorBoundary resetKey="inbox"><Bomb explode={false} /></ErrorBoundary>);

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

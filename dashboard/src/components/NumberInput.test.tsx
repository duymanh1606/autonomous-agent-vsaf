import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { NumberInput, numericOnly } from "./NumberInput";

describe("numericOnly", () => {
  it("keeps only digits for a count", () => {
    expect(numericOnly("aaaa")).toBe("");
    expect(numericOnly("-1a2 3.5")).toBe("1235");
  });

  it("keeps one decimal point for a price or a duration, and reads ',' as one", () => {
    expect(numericOnly("12,5", true)).toBe("12.5");
    expect(numericOnly("1.2.3", true)).toBe("1.23");
    expect(numericOnly("-abc", true)).toBe("");
  });
});

function Harness({ decimal }: { decimal?: boolean }) {
  const [value, setValue] = useState("");
  return <NumberInput aria-label="số" decimal={decimal} value={value} onValueChange={setValue} />;
}

describe("NumberInput", () => {
  it("never shows a letter, typed or pasted", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const input = screen.getByLabelText("số") as HTMLInputElement;

    await user.type(input, "aaaa");
    expect(input.value).toBe("");
    await user.click(input);
    await user.paste("12 cái");
    expect(input.value).toBe("12");
  });

  it("brings up a numeric keypad on phones", () => {
    render(<Harness decimal />);
    expect(screen.getByLabelText("số").getAttribute("inputmode")).toBe("decimal");
  });
});

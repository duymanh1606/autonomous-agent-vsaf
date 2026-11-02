import type { InputHTMLAttributes } from "react";

/**
 * Keep only what a non-negative number can contain: digits, plus - when
 * `decimal` - the first decimal point ("," counts as one, since that is what
 * a Vietnamese keyboard offers). Everything else is dropped as it is typed or
 * pasted, the minus sign included: none of these fields may go below zero.
 */
export function numericOnly(raw: string, decimal = false): string {
  if (!decimal) return raw.replace(/[^0-9]/g, "");
  const cleaned = raw.replace(/,/g, ".").replace(/[^0-9.]/g, "");
  const dot = cleaned.indexOf(".");
  return dot === -1 ? cleaned : cleaned.slice(0, dot + 1) + cleaned.slice(dot + 1).replace(/\./g, "");
}

type Props = Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "value" | "onChange" | "inputMode"> & {
  value: string;
  onValueChange: (value: string) => void;
  /** Allow one decimal point (prices, durations). Counts stay whole numbers. */
  decimal?: boolean;
};

/**
 * The dashboard's one input for quantities, amounts and durations.
 *
 * Text plus a filter, not `type="number"`: Firefox lets letters into a number
 * input and just reports its value as empty, so "aaaa" sat in the box looking
 * accepted while the form quietly saw nothing. Here a letter never gets in.
 * `inputMode` still brings up the numeric keypad on phones.
 */
export function NumberInput({ value, onValueChange, decimal = false, ...rest }: Props) {
  return (
    <input
      {...rest}
      type="text"
      inputMode={decimal ? "decimal" : "numeric"}
      value={value}
      onChange={(event) => onValueChange(numericOnly(event.target.value, decimal))}
    />
  );
}

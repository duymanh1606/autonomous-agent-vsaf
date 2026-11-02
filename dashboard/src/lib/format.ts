// Shared Vietnamese-locale currency formatting - previously three separate
// copies (Orders.tsx, Products.tsx, CommandPalette.tsx) had already drifted
// on the currency symbol ("đ" vs "₫"); one place to keep them in sync.

export function formatMoney(
  value: number | null | undefined,
  options?: { currency?: string; whenNull?: string },
): string {
  const { currency = "₫", whenNull = "" } = options ?? {};
  if (value == null) return whenNull;
  return `${value.toLocaleString("vi-VN")}${currency}`;
}

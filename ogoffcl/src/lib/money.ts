export const CURRENCY = "GHS";

export function money(n: number | null | undefined): string {
  const v = Number(n ?? 0);
  return `GH₵${v.toLocaleString("en-GH", { minimumFractionDigits: v % 1 ? 2 : 0, maximumFractionDigits: 2 })}`;
}

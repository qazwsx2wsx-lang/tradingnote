export function fmtYi(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const text = value.toFixed(digits);
  if (Number(text) === 0) return (0).toFixed(digits); // 避免 -0.00
  return `${value > 0 ? "+" : ""}${text}`;
}

export function fmtPct(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `${value > 0 ? "+" : ""}${value.toFixed(2)}%`;
}

export function streakLabel(n: number): string {
  if (n > 0) return `連買 ${n} 天`;
  if (n < 0) return `連賣 ${-n} 天`;
  return "—";
}

/** 紅＝買超、綠＝賣超（台股慣例） */
export function toneClass(value: number | null | undefined): string {
  if (!value) return "muted";
  return value > 0 ? "gain" : "loss";
}
